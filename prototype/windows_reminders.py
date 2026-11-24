"""Current-user Windows reminder scheduling and short-lived activation/delivery."""
import argparse
import base64
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from time import sleep, time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener
import winreg
import win32event
from xml.etree import ElementTree as ET

import pythoncom
from win32com.client import Dispatch
from win32com.propsys import propsys, pscon
from win32com.shell import shell, shellcon

from assistant_timers import TimerStore


ROOT = Path(__file__).resolve().parent
LOCAL_HTTP = build_opener(ProxyHandler({}))
VALUE_OPTIONS = ("engine_port", "model", "model_url", "memory_url", "write_delay", "qwen_backend", "vulkan_device")
FLAG_OPTIONS = ("allow_cpu", "draft_model_reply")
GENERATION_MARKER = "MOSS reminder generation "
PS = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$d = [Console]::In.ReadToEnd() | ConvertFrom-Json
$null = [Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]
$null = [Windows.UI.Notifications.ToastNotification,Windows.UI.Notifications,ContentType=WindowsRuntime]
$manager = [Windows.UI.Notifications.ToastNotificationManager]
if ($d.op -eq 'history') {
    # Windows PowerShell 不保证自动枚举 WinRT 返回的 COM 对象。
    $history = $manager::History.GetHistory($d.app)
    $notification = [Windows.UI.Notifications.ToastNotification]
    $collection = [System.Collections.Generic.IReadOnlyCollection`1].MakeGenericType($notification)
    $list = [System.Collections.Generic.IReadOnlyList`1].MakeGenericType($notification)
    $count = $collection.GetProperty('Count').GetValue($history)
    $item = $list.GetProperty('Item')
    $tag = $notification.GetProperty('Tag')
    $tags = for ($i = 0; $i -lt $count; $i++) {
        $tag.GetValue($item.GetValue($history, [object[]]@($i)))
    }
    $tags -join ','
} elseif ($d.op -eq 'remove') {
    foreach ($id in $d.ids) { $manager::History.Remove([string]$id, 'reminders', $d.app) }
} else {
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($d.xml)
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    $toast.Tag = [string]$d.tag
    $toast.Group = 'reminders'
    $toast.SuppressPopup = [bool]$d.quiet
    $notifier = $manager::CreateToastNotifier($d.app)
    if ($null -ne $notifier.Setting -and [int]$notifier.Setting -ne 0) { throw ('Notifications: ' + $notifier.Setting) }
    $notifier.Show($toast)
}
"""


@contextmanager
def com():
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def reminder_tag(key, generation):
    return str(key) if generation == 0 else f"{key}-{generation}"


def task_generation(task):
    description = task.Definition.RegistrationInfo.Description or ""
    marker = description.removeprefix(GENERATION_MARKER)
    if marker == description or not marker.isascii() or not marker.isdecimal() or len(marker) > 19:
        return 0
    return int(marker)


class WindowsReminders:
    def __init__(self, data, port, *, startup=None):
        self.data = Path(data).resolve()
        self.port = int(port)
        self.key = hashlib.sha256(os.path.normcase(str(self.data)).encode()).hexdigest()[:16]
        self.app = "MOSS.Reminders." + self.key
        self.scheme = "moss-reminder-" + self.key
        self.folder = "\\" + self.app
        self.shortcut = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_PROGRAMS, None, 0)) / (self.app + ".lnk")
        self.pythonw = Path(sys.executable).with_name("pythonw.exe")
        self.launch_file = self.data / "windows-reminder-launch.json"
        self.startup = None if startup is None else {
            name: getattr(startup, name) for name in (*VALUE_OPTIONS, *FLAG_OPTIONS) if getattr(startup, name, None) is not None}
        if self.startup is not None:
            self.startup["port"] = self.port
            if os.environ.get("MOSS_MEDIA_DB"):
                self.startup["media_db"] = str(Path(os.environ["MOSS_MEDIA_DB"]).resolve())
            if os.environ.get("MOSS_SPEECH_PREPARE") in {"0", "1"}:
                self.startup["speech_prepare"] = os.environ["MOSS_SPEECH_PREPARE"]
        elif self.launch_file.is_file():
            self.port = int(json.loads(self.launch_file.read_text(encoding="utf-8")).get("port", self.port))

    def command(self, action, argument, *, generation=0):
        command = [str(self.pythonw), "-X", "utf8", str(Path(__file__).resolve()), action, "--data", str(self.data),
                   "--port", str(self.port), "--value", str(argument)]
        if generation:
            command.extend(["--generation", str(generation)])
        return command

    def register(self):
        # A per-profile protocol lets Windows reopen the original instance after process exit.
        protocol = rf"Software\Classes\{self.scheme}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, protocol) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:MOSS reminder")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, protocol + r"\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ,
                              subprocess.list2cmdline(self.command("open", "%1")[:-1]) + ' "%1"')
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{self.app}") as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "MOSS 提醒")
        if not self.shortcut.exists():
            link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
            link.SetPath(str(self.pythonw))
            link.SetArguments(subprocess.list2cmdline(self.command("open", self.scheme + ":home")[1:]))
            link.SetDescription("MOSS 提醒")
            props = link.QueryInterface(propsys.IID_IPropertyStore)
            props.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(self.app))
            props.Commit()
            link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(self.shortcut), True)

    def scheduled_folder(self, service, create=False):
        try:
            return service.GetFolder(self.folder)
        except pythoncom.com_error as error:
            code = error.excepinfo[5] if error.excepinfo else error.hresult
            if code & 0xffff not in (2, 3):
                raise
        return service.GetFolder("\\").CreateFolder(self.app) if create else None

    def save_startup(self):
        if self.startup is not None:
            text = json.dumps(self.startup, ensure_ascii=False, sort_keys=True)
            if not self.launch_file.exists() or self.launch_file.read_text(encoding="utf-8") != text:
                temp = self.launch_file.with_suffix(".tmp")
                temp.write_text(text, encoding="utf-8")
                temp.replace(self.launch_file)

    def reconcile(self, rows):
        active = {key: (due, notified, generation) for key, due, notified, generation in rows}
        if not active and not self.shortcut.exists():
            return
        if active:
            self.save_startup()
        self._sync_tasks(active)

    @com()
    def _sync_tasks(self, active):
        service = Dispatch("Schedule.Service")
        service.Connect()
        folder = self.scheduled_folder(service, create=bool(active))
        if folder is None:
            return
        known = {int(task.Name): task_generation(task) for task in folder.GetTasks(0) if task.Name.isdecimal()}
        obsolete = known.keys() - active.keys()
        if obsolete:
            self.toast("remove", ids=[reminder_tag(key, known[key]) for key in sorted(obsolete)])
            for key in obsolete:
                folder.DeleteTask(str(key), 0)
        pending = [key for key, (_, notified, generation) in active.items()
                   if notified is None and known.get(key) != generation]
        if not pending:
            return
        self.register()
        for key in pending:
            deadline, _, generation = active[key]
            if key in known:
                # Keep the old generation discoverable until its notice has been withdrawn.
                self.toast("remove", ids=[reminder_tag(key, known[key])])
            now = time()
            due = math.ceil(deadline) if deadline > now else math.ceil(now) + 1
            task = service.NewTask(0)
            task.RegistrationInfo.Author = "MOSS"
            if generation:
                task.RegistrationInfo.Description = GENERATION_MARKER + str(generation)
            task.Principal.LogonType = 3  # Interactive token, current user.
            settings = task.Settings
            settings.StartWhenAvailable = True
            settings.WakeToRun = False
            settings.DisallowStartIfOnBatteries = False
            settings.StopIfGoingOnBatteries = False
            settings.ExecutionTimeLimit = "PT2M"
            settings.RestartInterval = "PT1M"
            settings.RestartCount = 2
            trigger = task.Triggers.Create(1)  # Time trigger.
            start = datetime.fromtimestamp(due, timezone.utc)
            trigger.StartBoundary = start.isoformat(timespec="seconds")
            end = start + min(timedelta(days=365), datetime.max.replace(tzinfo=timezone.utc) - start)
            trigger.EndBoundary = end.isoformat(timespec="seconds")
            action = task.Actions.Create(0)
            command = self.command("deliver", key, generation=generation)
            action.Path = command[0]
            action.Arguments = subprocess.list2cmdline(command[1:])
            folder.RegisterTaskDefinition(str(key), task, 6, "", "", 3)

    def toast(self, op, **fields):
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        payload = json.dumps({"app":self.app, "op":op, **fields}, ensure_ascii=False)
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                                 base64.b64encode(PS.encode("utf-16-le")).decode("ascii")],
                                input=payload, capture_output=True, encoding="utf-8", errors="replace",
                                timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError("Windows 通知中心未接受请求：" + result.stderr[:500])
        return result.stdout.strip()

    def owner(self):
        try:
            owner = json.loads((self.data / "instance.json").read_text(encoding="utf-8"))
            if owner["port"] != self.port:
                return None
            with LOCAL_HTTP.open(f"http://127.0.0.1:{self.port}/api/health", timeout=1) as res:
                health = json.load(res)
            if health["pid"] == owner["pid"] and health["app"] == "moss-local-chat":
                return owner
        except (OSError, ValueError, KeyError):
            pass
        return None

    def receipt(self, key, delivered, *, generation=0):
        for attempt in range(4):
            owner = self.owner()
            if owner:
                payload = json.dumps(dict(token=owner["token"], id=key, delivered=delivered,
                                          generation=generation)).encode()
                request = Request(f"http://127.0.0.1:{self.port}/api/timer-notification", data=payload,
                                  headers={"Content-Type":"application/json"})
                try:
                    with LOCAL_HTTP.open(request, timeout=2) as res:
                        return json.load(res)["timer"]
                except URLError:
                    if attempt == 3:
                        raise
            else:
                try:
                    with closing(sqlite3.connect((self.data / "session.sqlite3").as_uri() + "?mode=rw", uri=True, timeout=1)) as db, db:
                        return TimerStore(db).notification(key, delivered=delivered, generation=generation)
                except sqlite3.OperationalError:
                    if attempt == 3:
                        raise
            sleep(.25)

    def deliver(self, key, *, generation=0):
        item = self.receipt(key, False, generation=generation)
        if not item or item["delivered"]:
            return
        toast = ET.Element("toast", activationType="protocol", launch=f"{self.scheme}:open/{key}")
        binding = ET.SubElement(ET.SubElement(toast, "visual"), "binding", template="ToastGeneric")
        ET.SubElement(binding, "text").text = "MOSS 提醒"
        ET.SubElement(binding, "text").text = item["label"]
        # Tests opt into silent action-center delivery using a marker in their own data folder.
        quiet = (self.data / ".quiet-reminders").is_file()
        if quiet:
            ET.SubElement(toast, "audio", silent="true")
        tag = reminder_tag(key, generation)
        self.toast("show", tag=tag, xml=ET.tostring(toast, encoding="unicode"), quiet=quiet)
        if not self.receipt(key, True, generation=generation):
            self.toast("remove", ids=[tag])

    def activation_url(self, uri):
        match = re.fullmatch(re.escape(self.scheme) + r":(?:home|open/([1-9][0-9]{0,18}))", uri)
        if not match or (match[1] and int(match[1]) >= 2**63):
            raise ValueError("提醒链接无效")
        url = f"http://127.0.0.1:{self.port}/?chat=1"
        return url + ("#reminder=" + match[1] if match[1] else "")

    def ensure_server(self, *, startup_timeout=12):
        mutex = win32event.CreateMutex(None, False, "Local\\MOSS.Reminder.Open." + self.key)
        try:
            result = win32event.WaitForSingleObject(mutex, 15000)
            if result not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
                raise TimeoutError("MOSS 正在启动，请稍后打开。")
            try:
                owner = self.owner()
                if owner:
                    return owner["pid"]
                args = json.loads(self.launch_file.read_text(encoding="utf-8")) if self.launch_file.exists() else {}
                command = [str(self.pythonw), "-X", "utf8", str(ROOT / "serve.py"), "--data", str(self.data), "--port", str(self.port)]
                for key in VALUE_OPTIONS:
                    if args.get(key) is not None:
                        command.extend(["--" + key.replace("_", "-"), str(args[key])])
                for key in FLAG_OPTIONS:
                    if args.get(key) is True:
                        command.append("--" + key.replace("_", "-"))
                env = {**os.environ, "PYTHONUTF8":"1", "PYTHONIOENCODING":"utf-8", "PYTHONUNBUFFERED":"1"}
                if args.get("media_db"):
                    env["MOSS_MEDIA_DB"] = args["media_db"]
                if args.get("speech_prepare") in {"0", "1"}:
                    env["MOSS_SPEECH_PREPARE"] = args["speech_prepare"]
                proc = subprocess.Popen(command, cwd=ROOT, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
                for _ in range(math.ceil(startup_timeout / .15)):
                    owner = self.owner()
                    if owner:
                        return owner["pid"]
                    if proc.poll() is not None:
                        raise RuntimeError("MOSS 本机服务未能启动")
                    sleep(.15)
                raise TimeoutError("MOSS 本机服务未就绪")
            finally:
                win32event.ReleaseMutex(mutex)
        finally:
            mutex.Close()

    def activate(self, uri):
        url = self.activation_url(uri)
        self.ensure_server()
        os.startfile(url)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["deliver", "open"])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--value", required=True)
    parser.add_argument("--generation", type=int, default=0)
    args = parser.parse_args()
    reminders = WindowsReminders(args.data, args.port)
    try:
        if args.action == "deliver":
            reminders.deliver(int(args.value), generation=args.generation)
        else:
            reminders.activate(args.value)
    except Exception:
        logging.basicConfig(filename=reminders.data / "windows-reminder-error.log", encoding="utf-8", level=logging.ERROR)
        logging.exception("MOSS Windows reminder %s failed", args.action)
        raise
