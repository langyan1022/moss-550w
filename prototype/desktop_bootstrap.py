import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
from tempfile import NamedTemporaryFile
from time import monotonic, sleep
from uuid import uuid4

import pythoncom
from win32com.client import Dispatch

from windows_reminders import WindowsReminders, com


NO_PACKAGE = 15700
PACKAGE_PRESENT = 122
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WORKER_PREFIX = ".desktop-start-"
STARTUP_TIMEOUT = 32
KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
KERNEL.OpenProcess.restype = wintypes.HANDLE
KERNEL.CloseHandle.argtypes = (wintypes.HANDLE,)
KERNEL.CloseHandle.restype = wintypes.BOOL
KERNEL.GetCurrentPackageFullName.argtypes = (ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR)
KERNEL.GetCurrentPackageFullName.restype = wintypes.LONG
KERNEL.GetPackageFullName.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR)
KERNEL.GetPackageFullName.restype = wintypes.LONG


def has_package(pid=None):
    length = wintypes.UINT()
    if pid is None:
        code = KERNEL.GetCurrentPackageFullName(ctypes.byref(length), None)
    else:
        handle = KERNEL.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            code = KERNEL.GetPackageFullName(handle, ctypes.byref(length), None)
        finally:
            KERNEL.CloseHandle(handle)
    if code not in (NO_PACKAGE, PACKAGE_PRESENT):
        raise ctypes.WinError(code)
    return code == PACKAGE_PRESENT


def read_profile(data):
    profile = WindowsReminders(data, 18221)
    if not (profile.data / "session.sqlite3").is_file() or not profile.launch_file.is_file():
        raise ValueError("指定数据目录缺少 MOSS 会话数据库或已保存的启动配置。")
    return profile


def confirm_service(profile, pid):
    owner = profile.owner()
    if not owner or owner["pid"] != pid:
        raise RuntimeError("MOSS 服务未返回对应数据目录的身份回执。")
    if has_package(pid):
        raise RuntimeError("当前 MOSS 服务继承了 Windows 应用包身份。请先正常退出该服务。")
    return owner


def start_profile(profile):
    owner = profile.owner()
    pid = owner["pid"] if owner else profile.ensure_server(startup_timeout=STARTUP_TIMEOUT)
    confirm_service(profile, pid)
    return {"pid": pid, "port": profile.port, "reused": owner is not None}


def run_worker(profile, receipt):
    if receipt.parent != profile.data or not receipt.name.startswith(WORKER_PREFIX) or receipt.suffix != ".json":
        raise ValueError("启动回执必须位于指定的 MOSS 数据目录。")
    try:
        if has_package():
            raise RuntimeError("Windows 桌面启动进程继承了应用包身份。")
        result = start_profile(profile)
    except (OSError, RuntimeError, ValueError) as error:
        receipt.write_text(json.dumps({"error": str(error)}, ensure_ascii=False), encoding="utf-8")
        return 1
    receipt.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


@com()
def run_desktop_task(profile, receipt):
    service = Dispatch("Schedule.Service")
    service.Connect()
    root = service.GetFolder("\\")
    name = f"MOSS.DesktopBootstrap.{profile.key}.{uuid4().hex}"
    task = service.NewTask(0)
    task.RegistrationInfo.Author = "MOSS"
    task.Principal.LogonType = 3  # 使用当前用户的交互会话。
    task.Principal.RunLevel = 0
    task.Settings.ExecutionTimeLimit = "PT45S"
    task.Settings.DisallowStartIfOnBatteries = False
    task.Settings.StopIfGoingOnBatteries = False
    action = task.Actions.Create(0)
    action.Path = str(profile.pythonw)
    action.Arguments = subprocess.list2cmdline([
        "-X", "utf8", "-B", str(Path(__file__).resolve()), "--profile", str(profile.data), "--worker-receipt", str(receipt),
    ])
    registered = root.RegisterTaskDefinition(name, task, 2, "", "", 3)
    previous_run = registered.LastRunTime
    try:
        registered.Run("")
        deadline = monotonic() + 50
        while monotonic() < deadline:
            if registered.LastRunTime != previous_run and registered.State not in (2, 4):
                return registered.LastTaskResult
            sleep(.2)
        raise TimeoutError("Windows 桌面启动任务超时。")
    finally:
        if registered.State in (2, 4):
            registered.Stop(0)
        root.DeleteTask(name, 0)


def bootstrap(profile):
    owner = profile.owner()
    if owner:
        confirm_service(profile, owner["pid"])
        return {"pid": owner["pid"], "port": profile.port, "reused": True, "host": "existing_desktop"}
    if not has_package():
        return {**start_profile(profile), "host": "desktop"}
    with NamedTemporaryFile(prefix=WORKER_PREFIX, suffix=".json", dir=profile.data, delete=False) as stream:
        receipt = Path(stream.name)
    try:
        exit_code = run_desktop_task(profile, receipt)
        text = receipt.read_text(encoding="utf-8")
        result = json.loads(text) if text else {}
    finally:
        receipt.unlink(missing_ok=True)
    if exit_code != 0 or not result.get("pid"):
        raise RuntimeError(result.get("error", f"Windows 桌面启动任务未完成，返回代码 {exit_code}。"))
    confirm_service(profile, result["pid"])
    return {**result, "host": "windows_desktop_task"}


def main():
    parser = argparse.ArgumentParser(description="从当前 Windows 用户的桌面会话启动已有的 MOSS 数据目录。")
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--worker-receipt", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        profile = read_profile(args.profile)
        if args.worker_receipt:
            return run_worker(profile, args.worker_receipt.resolve())
        print(json.dumps(bootstrap(profile), ensure_ascii=False))
    except (OSError, RuntimeError, ValueError, pythoncom.com_error) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
