"""One isolated, reversible Windows Task Scheduler and notification-center integration check."""
import argparse
import asyncio
import ctypes
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime
from math import ceil
from pathlib import Path
from time import monotonic, sleep, time
from traceback import print_exc
from unittest.mock import patch
from xml.etree import ElementTree as ET
import winreg
import aiohttp
from win32com.client import Dispatch
from playwright.async_api import async_playwright
import psutil

from assistant_timers import parse_timer
from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser
from verify_stage_spectrum import tone_catalog
from windows_reminders import GENERATION_MARKER, WindowsReminders, com, reminder_tag, task_generation


NO_PACKAGE = 15700
PACKAGE_PRESENT = 122


def package_status():
    length = ctypes.c_uint32()
    return ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)


@com()
def task_list(profile):
    service = Dispatch("Schedule.Service")
    service.Connect()
    folder = profile.scheduled_folder(service)
    return {} if folder is None else {int(task.Name): task.Xml for task in folder.GetTasks(0)}


@com()
def cleanup(profile, ids):
    assert profile.data.is_relative_to(ROOT / "runs") and profile.shortcut.stem == profile.app
    service = Dispatch("Schedule.Service")
    service.Connect()
    folder = profile.scheduled_folder(service)
    tags = {str(key) for key in ids}
    if folder:
        for task in list(folder.GetTasks(0)):
            if task.Name.isdecimal():
                tags.add(reminder_tag(int(task.Name), task_generation(task)))
            folder.DeleteTask(task.Name, 0)
        service.GetFolder("\\").DeleteFolder(profile.app, 0)
    if profile.shortcut.exists() and tags:
        profile.toast("remove", ids=sorted(tags))
    profile.shortcut.unlink(missing_ok=True)
    protocol = rf"Software\Classes\{profile.scheme}"
    for path in [protocol + child for child in (r"\shell\open\command", r"\shell\open", r"\shell", "")] + [
            rf"Software\Classes\AppUserModelId\{profile.app}",
            rf"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\{profile.app}"]:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass


async def run_reschedule(folder, port):
    server = BrowserServer(folder, port, model_url="http://127.0.0.1:1/v1")
    profile = WindowsReminders(server.data, port)
    assert profile.data.is_relative_to(ROOT / "runs")
    server.data.mkdir()
    (server.data / ".quiet-reminders").touch()
    report = {"passed": False, "profile": profile.app}
    tags = set()

    async def send(text):
        status, state = await server.post("/api/message", {"text": text})
        assert status == 200 and not state.get("timer_windows_error"), state["turns"][-1]
        assert state["turns"][-1]["status"] == "replied", state["turns"][-1]
        assert state["turns"][-1]["cloud_requests"]["total"] == 0
        return state

    def read_row(key):
        with closing(sqlite3.connect(server.data / "session.sqlite3", timeout=2)) as db:
            return db.execute("SELECT due_at,generation,notified_at FROM assistant_timers WHERE id=?", (key,)).fetchone()

    def task_boundary(xml):
        raw = ET.fromstring(xml).findtext(".//{*}TimeTrigger/{*}StartBoundary")
        return datetime.fromisoformat(raw).timestamp()

    async def history():
        return (await asyncio.to_thread(profile.toast, "history")).split(",")

    try:
        catalog = tone_catalog(folder)
        with patch.dict(os.environ, MOSS_WINDOWS_REMINDERS="1",
                        MOSS_SPEECH_PREPARE="0", MOSS_SPEECH_STANDBY="0",
                        MOSS_MEM0_ENABLED="0", MOSS_KEV_ENABLED="0", MOSS_MEDIA_DB=str(catalog),
                        MOSS_PROVIDER_DIR=str(folder / "providers"), LOCALAPPDATA=str(folder / "profile")):
            await server.start()
            assert task_list(profile) == {} and not profile.shortcut.exists()
            initial = await send("15秒后提醒我验收改期")
            original, = initial["timers"]
            key = original["id"]
            legacy_tag, moved_tag = reminder_tag(key, 0), reminder_tag(key, 1)
            tags.update((legacy_tag, moved_tag))
            first = task_list(profile)
            assert set(first) == {key}
            assert task_boundary(first[key]) == ceil(original["due_at"])
            assert "--generation" not in ET.fromstring(first[key]).findtext(".//{*}Exec/{*}Arguments")
            result = await send("把刚才的提醒改到30秒后")
            moved, = result["timers"]
            assert (moved["id"], moved["label"], moved["chat_id"], moved["turn_id"]) == (
                key, original["label"], original["chat_id"], original["turn_id"])
            assert moved["due_at"] > original["due_at"] + 10
            second = task_list(profile)
            assert set(second) == {key}
            assert task_boundary(second[key]) == ceil(moved["due_at"])
            xml = ET.fromstring(second[key])
            assert xml.findtext(".//{*}RegistrationInfo/{*}Description") == GENERATION_MARKER + "1"
            assert "--generation 1" in xml.findtext(".//{*}Exec/{*}Arguments")
            await server.stop()
            assert read_row(key) == (moved["due_at"], 1, None)
            await server.start()
            assert task_list(profile) == second and (await server.state())["timers"] == [moved]
            await server.stop()

            await asyncio.sleep(max(0, original["due_at"] + 1.25 - time()))
            assert not profile.owner() and time() < moved["due_at"]
            await asyncio.to_thread(profile.deliver, key, generation=0)
            observed = await history()
            assert legacy_tag not in observed and moved_tag not in observed
            assert read_row(key) == (moved["due_at"], 1, None)
            report.update(same_id=key, previous_due=original["due_at"], moved_due=moved["due_at"],
                          task_start=task_boundary(second[key]), unchanged_after_restart=True, old_time_silent=True)

            deadline = moved["due_at"] + 30
            acknowledged = None
            while time() < deadline:
                _, _, acknowledged = read_row(key)
                if acknowledged and moved_tag in (observed := await history()):
                    break
                await asyncio.sleep(.6)
            else:
                raise AssertionError("The rescheduled native notification did not arrive")
            assert acknowledged >= moved["due_at"] and observed.count(moved_tag) == 1 and legacy_tag not in observed
            assert await asyncio.to_thread(profile.receipt, key, True, generation=0) is None
            delivered = await asyncio.to_thread(profile.receipt, key, False, generation=1)
            assert delivered and delivered["delivered"]
            await asyncio.to_thread(profile.deliver, key, generation=0)
            assert (await history()).count(moved_tag) == 1

            await server.start()
            token = profile.owner()["token"]
            code, old = await server.post("/api/timer-notification", {"token": token, "id": key, "delivered": True})
            assert code == 200 and old["timer"] is None
            code, current = await server.post("/api/timer-notification", {"token": token, "id": key, "generation": 1})
            assert code == 200 and current["timer"]["delivered"]
            async with aiohttp.ClientSession() as client:
                for invalid in (-1, True, 2**63, "1"):
                    payload = {"token": token, "id": key, "generation": invalid}
                    async with client.post(server.url + "/api/timer-notification", json=payload) as response:
                        assert response.status == 400

            # Move a delivered-but-open reminder and withdraw only the old toast tag.
            latest, = (await send("把刚才的提醒改到明早九点"))["timers"]
            latest_tag = reminder_tag(key, 2)
            tags.add(latest_tag)
            assert latest["id"] == key and latest["due_at"] > time()
            tasks = task_list(profile)
            assert set(tasks) == {key} and task_boundary(tasks[key]) == ceil(latest["due_at"])
            assert "--generation 2" in ET.fromstring(tasks[key]).findtext(".//{*}Exec/{*}Arguments")
            observed_after_move = await history()
            assert moved_tag not in observed_after_move and latest_tag not in observed_after_move
            await send(f"取消提醒{key}")
            assert task_list(profile) == {} and moved_tag not in await history()
            await server.stop()
            assert read_row(key) == (latest["due_at"], 2, None)
            report.update(passed=True, delivered_at=acknowledged, closed_process_delivery=True,
                          old_generation_rejected=True, centre_tags=[tag for tag in observed if tag],
                          moved_delivered_reminder_and_withdrew_toast=True, cancelled_native_task=True)
    finally:
        await server.stop()
        cleanup(profile, tags)
        report["sandbox_cleaned"] = task_list(profile) == {} and not profile.shortcut.exists()
        assert report["sandbox_cleaned"]
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


async def run(folder, port):
    server = BrowserServer(folder, port, model_url="http://127.0.0.1:1/v1")
    profile = WindowsReminders(server.data, port)
    server.data.mkdir()
    (server.data / ".quiet-reminders").touch()
    report = {"folder":str(folder), "profile":profile.app, "events":[]}
    test_ids = set()

    def event(name, **extra):
        report["events"].append({"name":name, **extra})
        (folder / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(name, flush=True)

    async def send(text):
        status, state = await server.post("/api/message", {"text":text})
        test_ids.update(timer["id"] for timer in state.get("timers", []))
        assert status == 200 and state.get("timer_windows_error") is None, state["turns"][-1]["reply"]
        return state

    async def stop_activated():
        owner = profile.owner()
        if owner:
            process = psutil.Process(owner["pid"])
            await server.post("/api/shutdown", {"token":owner["token"]})
            await asyncio.to_thread(process.wait, 8)

    try:
        dates = {text: (parse_timer(text)["day"], parse_timer(text)["hour"]) for text in [
            "明早九点提醒我继续听这首歌", "今晚九点提醒我休息", "明晚七点提醒我休息", "后天一早八点提醒我休息"]}
        assert list(dates.values()) == [("明天",9), ("今天",21), ("明天",19), ("后天",8)]
        event("common absolute time expressions", parsed=dates)
        index = tone_catalog(folder)
        with patch.dict(os.environ, MOSS_WINDOWS_REMINDERS="1", MOSS_MEDIA_DB=str(index)):
            await server.start()
            assert not profile.shortcut.exists() and task_list(profile) == {}
            async with aiohttp.ClientSession() as client:
                async with client.post(server.url+"/api/timer-notification", json={"token":"wrong","id":1}) as response:
                    assert response.status == 403
            event("empty startup and authenticated delivery")
            state = await send("12秒后提醒我测试静默到达")
            due = state["timers"][0]
            assert due["chat_id"] == state["chat_id"] and due["turn_id"] == state["turns"][-1]["id"]
            first = task_list(profile)
            assert set(first) == {1} and "StartWhenAvailable>true<" in first[1] and "WakeToRun>true<" not in first[1]
            assert profile.shortcut.exists()
            event("native scheduled", id=due["id"], due_at=due["due_at"])
            await send("2小时后提醒我验证取消")
            assert set(task_list(profile)) == {1, 2}
            await send("取消提醒2")
            assert task_list(profile) == first
            event("individual cancellation")
            await server.stop()
            await server.start()
            assert task_list(profile) == first
            event("restart preserves single original task", pid=server.pid)
            await server.stop()
            deadline = due["due_at"] + 30
            seen = None
            while not seen and time() < deadline:
                await asyncio.sleep(.7)
                with closing(sqlite3.connect(server.data / "session.sqlite3", timeout=2)) as db:
                    seen, = db.execute("select notified_at from assistant_timers where id=1").fetchone()
            if not seen:
                service = Dispatch("Schedule.Service")
                service.Connect()
                task = profile.scheduled_folder(service).GetTask("1")
                event("delivery failed", result=task.LastTaskResult, last=str(task.LastRunTime), next=str(task.NextRunTime))
                raise AssertionError("Windows worker did not acknowledge delivery to closed-app SQLite")
            event("scheduled worker acknowledged toast submission", due_at=due["due_at"], sqlite_ack=seen)
            history = []
            while time() < deadline:
                history = (await asyncio.to_thread(profile.toast, "history")).split(",")
                if "1" in history:
                    break
                await asyncio.sleep(.7)
            if "1" not in history or "2" in history:
                event("WinRT history did not return expected tags", center_tags=[tag for tag in history if tag])
                raise AssertionError(f"Unexpected notification history after submission: {history}")
            event("delivered with app stopped", due_at=due["due_at"], sqlite_ack=seen, center_tags=history)
            profile.deliver(1)
            assert profile.toast("history").split(",").count("1") == 1
            event("duplicate firing keeps one notification")
            uri = profile.scheme + ":open/1"
            assert profile.activation_url(uri) == server.url + "/?chat=1#reminder=1"
            try:
                profile.activation_url(profile.scheme + ":open/1&other")
            except ValueError:
                pass
            else:
                raise AssertionError("bad notification URI accepted")
            await server.start()
            state = await server.state()
            assert len(state["timers"]) == 1
            status, _ = await server.post("/api/timer-dismiss", {"id":1})
            assert status == 200 and not (await server.state())["timers"] and not task_list(profile)
            assert "1" not in profile.toast("history").split(",")
            event("reopened reminder and dismissed across SQLite, scheduler, notification center")
            live = await send("3秒后提醒我在线静默测试")
            key = live["timers"][0]["id"]
            deadline = time() + 25
            while time() < deadline:
                if str(key) in (await asyncio.to_thread(profile.toast, "history")).split(","):
                    break
                await asyncio.sleep(.5)
            else:
                raise AssertionError("Windows notification was not found while the server was running")
            status, _ = await server.post("/api/timer-dismiss", {"id":key})
            assert status == 200 and not task_list(profile)
            event("delivery with server live, browser closed", id=key)
            await send("频谱测试员的双音阶梯")
            for _ in range(50):
                state = await server.state()
                if state["turns"][-1]["status"] == "replied":
                    break
                await asyncio.sleep(.1)
            track = state["turns"][-1]
            media_id = track["media"]["selected"]["media_id"]
            status, _ = await server.post("/api/media-event", {"chat_id":state["chat_id"], "turn_id":track["id"],
                "media_id":media_id, "event":"started", "play_id":"aa2ffd14186846cba6c3af338d61dd48"})
            assert status == 200
            state = await send("明早九点提醒我继续听这首歌")
            assert state["timers"][0]["media"]["media_id"] == media_id
            await send("取消全部提醒")
            state = await send("3秒后提醒我继续听这首歌")
            reminder = state["timers"][0]
            assert reminder["media"]["turn_id"] == track["id"]
            await server.post("/api/new-chat", {})
            await server.stop()
            await asyncio.sleep(max(0, reminder["due_at"] - time()) + 1)
            # Exercise activation's actual pythonw boot while avoiding the user's default browser.
            pids = await asyncio.gather(*(asyncio.to_thread(profile.ensure_server) for _ in range(2)))
            assert pids[0] == pids[1] and profile.owner()["pid"] == pids[0]
            argv = psutil.Process(pids[0]).cmdline()
            assert any("pythonw" in p.lower() for p in argv) and server.model_url in argv and server.model in argv
            restored = json.loads(profile.launch_file.read_text(encoding="utf-8"))
            assert restored["media_db"] == str(index) and restored["engine_port"] == 18222
            event("two activations started one windowless process with restored settings", pid=pids[0])
            uri = profile.activation_url(profile.scheme + ":open/" + str(reminder["id"]))
            async with async_playwright() as playwright:
                browser = await open_browser(playwright)
                try:
                    await confirm_muted_edge(browser)
                    page = await browser.new_page(reduced_motion="reduce")
                    await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                    await page.route("**/conversation-assets/{moss-view,earth-view}.js", lambda route: route.abort())
                    await page.goto(uri)
                    source = page.get_by_test_id("timer-source")
                    await source.wait_for(timeout=6000)
                    assert "双音阶梯" in await source.inner_text()
                    await source.click()
                    await page.wait_for_function("""id => Array.from(document.querySelectorAll('.media-audio'))
                        .some(a => a.dataset.mediaId === id && !a.paused && a.currentTime > .05)""",
                        arg=media_id, timeout=7000)
                    latest = await server.state()
                    assert latest["chat_id"] == reminder["media"]["chat_id"]
                    assert latest["turns"][-1]["media"]["selected"]["media_id"] == media_id
                    await page.get_by_test_id("timer-dismiss").click()
                    await page.get_by_test_id("timer-notice").wait_for(state="hidden")
                    event("notification deep link restored source chat and played exact archived media", media_id=media_id,
                          original_turn=track["id"], index=reminder["media"]["index"])
                finally:
                    await browser.close()
            await stop_activated()
            # Old-schema/committed reminders can predate installation of the Windows integration.
            with closing(sqlite3.connect(server.data / "session.sqlite3")) as db, db:
                missed = db.execute("INSERT INTO assistant_timers(label,due_at) VALUES(?,?)",
                                    ("补办先前错过的提醒", time() - 7200)).lastrowid
            test_ids.add(missed)
            await server.start()
            deadline = time() + 15
            while time() < deadline:
                if str(missed) in (await asyncio.to_thread(profile.toast, "history")).split(","):
                    break
                await asyncio.sleep(.35)
            else:
                raise AssertionError("Overdue persisted reminder was not caught up at startup")
            await server.post("/api/timer-dismiss", {"id":missed})
            event("overdue reminder reconciled from SQLite after restarting", hours_late=2)
    finally:
        await server.stop()
        await stop_activated()
        cleanup(profile, test_ids)
        event("released test server, task folder, toast, protocol and shortcut")


def run_desktop_worker(folder, port, reschedule_only):
    assert folder.parent == ROOT / "runs" and folder.name.startswith("windows-reminders-")
    with (folder / "desktop-worker.log").open("w", encoding="utf-8") as log, redirect_stdout(log), redirect_stderr(log):
        try:
            status = package_status()
            print(f"GetCurrentPackageFullName={status}", flush=True)
            if status != NO_PACKAGE:
                raise RuntimeError("提醒验收进程继承了 Windows 应用包身份。")
            asyncio.run((run_reschedule if reschedule_only else run)(folder, port))
        except Exception:
            print_exc()
            return 1
    return 0


@com()
def run_from_packaged_host(folder, port, reschedule_only):
    service = Dispatch("Schedule.Service")
    service.Connect()
    root = service.GetFolder("\\")
    name = "MOSS.Test." + folder.name
    task = service.NewTask(0)
    task.RegistrationInfo.Author = "MOSS QA"
    task.Principal.LogonType = 3
    task.Settings.DisallowStartIfOnBatteries = False
    task.Settings.StopIfGoingOnBatteries = False
    task.Settings.ExecutionTimeLimit = "PT3M"
    action = task.Actions.Create(0)
    action.Path = str(Path(sys.executable).with_name("pythonw.exe"))
    command = [
        "-X", "utf8", "-B", str(Path(__file__).resolve()), "--port", str(port), "--desktop-worker", str(folder),
    ]
    if reschedule_only:
        command.append("--reschedule-only")
    action.Arguments = subprocess.list2cmdline(command)
    # 继承 Codex 等 MSIX 包身份会让 WinRT 查询另一个应用的通知。验收任务使用当前用户的桌面会话。
    registered = root.RegisterTaskDefinition(name, task, 2, "", "", 3)
    previous_run = registered.LastRunTime
    try:
        registered.Run("")
        deadline = monotonic() + 180
        while monotonic() < deadline:
            if registered.LastRunTime != previous_run and registered.State not in (2, 4):
                break
            sleep(.3)
        exited = registered.State not in (2, 4) and registered.LastRunTime != previous_run
        exit_code = registered.LastTaskResult if exited else 1
    finally:
        if registered.State in (2, 4):
            registered.Stop(0)
        root.DeleteTask(name, 0)
    log = folder / "desktop-worker.log"
    if log.exists():
        print(log.read_text(encoding="utf-8"), end="")
    (folder / "desktop-worker-result.json").write_text(
        json.dumps({"exited": exited, "exitCode": exit_code, "temporaryTaskRemoved": True}, indent=2) + "\n",
        encoding="utf-8",
    )
    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18995)
    parser.add_argument("--desktop-worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--reschedule-only", action="store_true")
    args = parser.parse_args()
    if args.desktop_worker:
        raise SystemExit(run_desktop_worker(args.desktop_worker.resolve(), args.port, args.reschedule_only))
    status = package_status()
    if status not in (PACKAGE_PRESENT, NO_PACKAGE):
        raise ctypes.WinError(status)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = ROOT / "runs" / ("windows-reminders-" + stamp)
    folder.mkdir(parents=True, exist_ok=False)
    if status == PACKAGE_PRESENT:
        raise SystemExit(run_from_packaged_host(folder, args.port, args.reschedule_only))
    asyncio.run((run_reschedule if args.reschedule_only else run)(folder, args.port))
