import argparse
import json
import os
from pathlib import Path
import re
from time import perf_counter
from urllib.error import HTTPError
from urllib.request import Request

import win32event

from desktop_bootstrap import bootstrap
from windows_reminders import LOCAL_HTTP, WindowsReminders


def send(windows, action, *, listen_next=False):
    owner = windows.owner()
    if owner is None:
        raise RuntimeError("MOSS 本机服务未就绪。")
    payload = {"token": owner["token"]}
    if listen_next:
        payload["listen_next"] = True
    request = Request(f"http://127.0.0.1:{windows.port}/api/wake/{action}",
                      data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    try:
        with LOCAL_HTTP.open(request, timeout=2) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 404:
            raise SystemExit("请重新启动 MOSS 服务以载入 Windows 唤醒功能。") from None
        raise


def activate(windows, *, probe=False, listen_next=False):
    mutex = win32event.CreateMutex(None, False, "Local\\MOSS.Wake.Activation." + windows.key)
    try:
        result = win32event.WaitForSingleObject(mutex, 0)
        if result == win32event.WAIT_TIMEOUT:
            return {"result": "in_progress"}
        if result not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
            raise RuntimeError("无法取得 MOSS 唤醒使用权。")
        try:
            start = perf_counter()
            owner = windows.owner()
            if owner:
                pid = owner["pid"]
            elif windows.launch_file.is_file():
                pid = bootstrap(windows)["pid"]
            else:
                pid = windows.ensure_server()
            ready = perf_counter()
            receipt = send(windows, "activate", listen_next=listen_next)
            result = receipt["result"]
            page = "reused"
            launch = {}
            if result != "page_shown":
                page = "default_browser_required" if probe else "default_browser_requested"
                if not probe or listen_next:
                    url = windows.activation_url(windows.scheme + ":home")
                    ticket = receipt.get("launch_ticket") if listen_next else None
                    if isinstance(ticket, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ticket):
                        url += "#native-wake=" + ticket
                    if probe:
                        launch["activation_url"] = url
                    else:
                        try:
                            os.startfile(url)
                        except OSError:
                            raise RuntimeError("Windows 无法打开 MOSS 对话。") from None
            return {"result": page, "receipt": result, "pid": pid, "port": windows.port, **launch,
                    "server_ms": round((ready - start) * 1000),
                    "handoff_ms": round((perf_counter() - ready) * 1000)}
        finally:
            win32event.ReleaseMutex(mutex)
    finally:
        mutex.Close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--browser-ready", action="store_true", help="仅查询是否有已连接的 MOSS 网页")
    mode.add_argument("--probe", action="store_true", help="仅验证网页接力，不调用 Windows 打开入口")
    parser.add_argument("--listen-next", action="store_true", help="识别唤醒词且释放麦克风后请求网页接听下一句")
    args = parser.parse_args()
    if args.browser_ready and args.listen_next:
        parser.error("查询浏览器状态不能同时请求接听。")
    if not (args.profile / "session.sqlite3").is_file():
        parser.error("指定目录没有 MOSS 会话资料。")
    windows = WindowsReminders(args.profile, 18221)
    if args.browser_ready:
        result = bool(windows.owner() and send(windows, "status")["page_connected"])
        print(json.dumps({"page_connected": result}))
        return 0 if result else 2
    print(json.dumps(activate(windows, probe=args.probe, listen_next=args.listen_next)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
