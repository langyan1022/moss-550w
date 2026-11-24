from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from time import monotonic
from urllib.request import Request

import psutil
import win32api
import win32con
import win32security
import winreg
from win32com.client import Dispatch

from desktop_bootstrap import PROCESS_QUERY_LIMITED_INFORMATION, STARTUP_TIMEOUT, has_package, read_profile
from windows_reminders import LOCAL_HTTP, com


ROOT = Path(__file__).resolve().parent


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fixture(folder):
    folder.mkdir()
    with closing(sqlite3.connect(folder / "session.sqlite3")) as db:
        db.execute("PRAGMA user_version")
    config = {"port": free_port(), "model_url": "http://127.0.0.1:1/v1", "model": "moss-isolated-desktop-test",
              "engine_port": free_port(), "media_db": str(folder / "empty-media.sqlite3")}
    (folder / "windows-reminder-launch.json").write_text(json.dumps(config), encoding="utf-8")
    return read_profile(folder), config


def call(profile):
    return subprocess.run([sys.executable, "-X", "utf8", "-B", str(ROOT / "desktop_bootstrap.py"),
                           "--profile", str(profile.data)], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=65)


def stop(profile):
    owner = profile.owner()
    if not owner:
        return
    process = psutil.Process(owner["pid"])
    request = Request(f"http://127.0.0.1:{profile.port}/api/shutdown",
                      data=json.dumps({"token": owner["token"]}).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    with LOCAL_HTTP.open(request, timeout=3) as response:
        assert response.status == 200
    process.wait(8)


@com()
def task_names(profile):
    service = Dispatch("Schedule.Service")
    service.Connect()
    prefix = f"MOSS.DesktopBootstrap.{profile.key}."
    return [task.Name for task in service.GetFolder("\\").GetTasks(0) if task.Name.startswith(prefix)]


def token_details(pid):
    handle = win32api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    try:
        token = win32security.OpenProcessToken(handle, win32con.TOKEN_QUERY)
        try:
            user, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
            return win32security.ConvertSidToStringSid(user), bool(
                win32security.GetTokenInformation(token, win32security.TokenElevation))
        finally:
            token.Close()
    finally:
        handle.Close()


def main():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = ROOT / "runs" / ("desktop-bootstrap-" + stamp)
    folder.mkdir(parents=True)
    profile, config = fixture(folder / "native")
    packaged = has_package()
    report = {"packagedCaller": packaged, "browserStarted": False, "passed": False}
    try:
        start = monotonic()
        result = call(profile)
        assert result.returncode == 0, result.stderr
        native = json.loads(result.stdout)
        report["start"] = {**native, "seconds": monotonic() - start}
        assert native["port"] == config["port"] and not native["reused"]
        assert native["host"] == ("windows_desktop_task" if packaged else "desktop")
        pid = native["pid"]
        assert not task_names(profile) and not list(profile.data.glob(".desktop-start-*.json"))
        assert profile.owner()["pid"] == pid and not has_package(pid)
        argv = psutil.Process(pid).cmdline()
        assert config["model_url"] in argv and config["model"] in argv and str(config["engine_port"]) in argv
        saved = json.loads(profile.launch_file.read_text(encoding="utf-8"))
        assert all(saved.get(key) == value for key, value in config.items())
        assert not profile.shortcut.exists()
        sid, elevated = token_details(pid)
        caller_sid, caller_elevated = token_details(os.getpid())
        assert sid == caller_sid
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System") as key:
            uac_enabled = bool(winreg.QueryValueEx(key, "EnableLUA")[0])
        report["nativeServer"] = {"hasPackage": False, "isElevated": elevated, "savedConfig": True,
                                  "launchTaskRemoved": True, "profileShortcutCreated": False,
                                  "sameUser": True, "callerElevated": caller_elevated, "uacEnabled": uac_enabled}
        if packaged:
            assert elevated == (caller_elevated and not uac_enabled)
        reused = call(profile)
        assert reused.returncode == 0, reused.stderr
        again = json.loads(reused.stdout)
        assert again["pid"] == pid and again["reused"] and again["host"] == "existing_desktop"
        assert not task_names(profile)
        report["reused"] = again
        if packaged:
            existing, _ = fixture(folder / "packaged")
            try:
                original = existing.ensure_server(startup_timeout=STARTUP_TIMEOUT)
                assert has_package(original)
                refused = call(existing)
                assert refused.returncode == 1 and "继承了 Windows 应用包身份" in refused.stderr
                assert existing.owner()["pid"] == original and not task_names(existing)
                report["existingPackagedInstance"] = {"refused": True, "retained": True}
            finally:
                stop(existing)
        report["passed"] = True
    finally:
        stop(profile)
        report["isolatedServerStopped"] = profile.owner() is None
        path = folder / "result.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(path)


if __name__ == "__main__":
    main()
