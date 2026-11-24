import asyncio
import io
import os
import re
import subprocess
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from device_status import device_status, parse_device_status
from directory_tools import DIRECTORY_LIMIT, DIRECTORY_PAGE, list_directory, read_directory
from file_organization_apply import snapshot_file
from local_calendar import calendar_followup, calendar_list_window
from local_overview import parse_local_overview, parse_mcp_overview, run_local_overview
from mcp_configured import parse_configured_mcp, parse_natural_mcp, run_configured_mcp
from mcp_media import parse_media_mcp, run_media_mcp
from system_media import run_system_media, system_media_intent
from weather_forecast import WEATHER_DAYS


MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 8 * 1024 * 1024
OCR_LIMIT = asyncio.Semaphore(2)
PREFIX = r"^(?:(?:请你?|能不能|能否|可以|帮我|麻烦你?|给我)\s*)*"
LOCAL_ACTIONS = {
    "clipboard": r"(?:(?:读(?:取|一下)?|看(?:看|一下)?|查看|显示|检查|粘贴)(?:我的?|当前的?)?剪[贴切]板(?:里(?:面)?的?(?:文字|内容))?|(?:我的?|当前的?)?剪[贴切]板(?:里(?:面)?(?:有|是)什么|的内容是什么))",
    "screenshot": r"(?:截(?:个|一张|一下)?(?:屏|屏幕|图)(?:幕)?|截取(?:当前)?(?:屏幕|窗口)(?:的)?(?:画面|截图)?|(?:给|为)?(?:当前|这个)?(?:窗口|屏幕)截(?:个|一张)?图|保存(?:一张)?(?:当前)?屏幕截图)",
    "screen_image": r"(?:(?:看(?:看|一下)?|查看|分析|解读)(?:当前|这个|所选)?窗口(?:的)?(?:画面|图像|截图)|对(?:当前|这个|所选)?窗口(?:画面|截图)提问)",
    "screen_text": r"(?:读(?:一下|取)?(?:当前|一下)?(?:我的)?(?:这个|当前)?(?:屏幕|窗口)(?:上|里)?(?:的)?(?:文字|内容)?|(?:识别|提取|看看|查看)(?:一下)?(?:当前|我的|这个)?(?:屏幕|窗口)(?:里|上)?(?:的)?(?:文字|内容|写了什么)|读屏|(?:当前|我的)?屏幕上写了什么)",
}
DIRECTORY_VERB = r"(?:列出|列一下|列一列|列|看看|看一下|查看|显示|看)"
DIRECTORY_SUFFIX = r"(?:里(?:面)?|下(?:面)?)?(?:都有(?:什么|哪些)|有(?:什么|哪些)|包含哪些)(?:文件(?:和文件夹)?|子目录|内容|东西)?"
NEXT_PAGE = r"(?:(?:看看|显示|翻到|查看)?下(?:一|1)页|继续列(?:出)?|还有哪些(?:文件|项目)?|剩下的(?:文件|项目)?(?:呢|还有哪些)?)"
BY_FILE_TYPE = r"按(?:照)?(?:文件)?(?:类型|后缀|扩展名)"
ORGANIZE_FILES = r"(?:整理|分类|分组)(?:一下)?"
USE_FILE_PLAN = r"(?:按(?:整理)?(?:计划|预览)|照(?:计划|预览))\s*(?:归类|归档)"
PLAN_SUFFIX = re.compile(
    r"(?:\s*[,，、]\s*|\s*)(?:先(?:给我)?(?:看(?:看|一下)?(?:整理|分类)?(?:计划|方案|预览)"
    r"|预览(?:一下)?|(?:来|给)?(?:一份|一个)(?:计划|方案))|只(?:做|看)(?:计划|预览))(?:吧)?$"
)


def parse_context(text, previous=None, *, nearby_location=None):
    spoken = re.sub(PREFIX, "", text.strip())
    request = spoken.rstrip("。！？!? ").strip()
    page = previous.get("directory_page") if previous and previous.get("source") == "context" and previous.get("status") == "replied" else None
    directory_named = re.fullmatch(r"(?:这个|该)?(?:目录|文件夹)(?:的)?下一页", request)
    if directory_named or (request == "下一页" and not previous) or (page is not None and re.fullmatch(NEXT_PAGE + r"(?:吧|呢)?", request)):
        return {"action": "directory", "page": page}
    if overview := parse_mcp_overview(request, nearby_location=nearby_location):
        return overview
    if mcp := (parse_media_mcp(request) or parse_configured_mcp(request, question=spoken.endswith(("?", "？")))
               or parse_natural_mcp(text)):
        return mcp
    if device := parse_device_status(request):
        return device
    if operation := system_media_intent(text):
        return {"action": "system_media", "operation": operation}
    for action, pattern in LOCAL_ACTIONS.items():
        if re.fullmatch(pattern + r"(?:吧|吗)?", request):
            return {"action": action}
    move = (re.fullmatch(rf"{USE_FILE_PLAN}\s*[:：]?\s*(.*)", request)
            or re.fullmatch(rf"(?:把|将)\s*(.+?)\s*{USE_FILE_PLAN}", request))
    if move:
        source = previous if previous and previous.get("source") == "context" and previous.get("status") == "replied" else {}
        name = move[1].strip().strip('"“”`「」').strip()
        return {"action":"file_plan_apply", "name":name, "plan":source.get("file_plan"),
                "focus":source.get("directory_focus"), "snapshots":source.get("_file_plan_snapshots", {})}
    if organization := parse_file_plan(request, previous, page):
        return organization
    if overview := parse_local_overview(request, nearby_location=nearby_location):
        return overview
    match = re.fullmatch(rf"({DIRECTORY_VERB})\s*(.+)", request)
    verb = match[1] if match else ""
    target = match[2].strip() if match else request
    suffix = re.search(rf"(?:{DIRECTORY_SUFFIX}|(?:里(?:面)?|下(?:面)?)(?:的)?(?:文件和文件夹|目录内容|文件|内容)|的(?:目录列表|文件列表|内容))$", target)
    if suffix:
        target = target[:suffix.start()].strip()
    elif not match:
        return None
    # Do not hijack natural analytical / file-reading prompts that start with a path
    # (e.g. "帮我看看 D:\docs 目录里最新修改的文件写了什么" or "看看 D:\notes\report.md").
    if re.search(
        r"(?:\s+|[,，。；;])(?:目录|文件夹)(?:里|中|内|下)"
        r"|(?:写了什么|核心内容|总结|概括|分析|解读|读取|读一下|最新修改|最近修改|里程碑|顺便|有没有报错)",
        target,
    ):
        return None
    if verb and not re.fullmatch(r"列出|列一下|列一列|列", verb):
        if re.search(
            r"\.(?:md|txt|py|js|ts|jsx|tsx|json|ya?ml|log|csv|ini|toml|html?|css|sql|xml|sh|ps1|bat|cfg|conf)$",
            target.strip('"“”`'),
            re.I,
        ):
            return None
    return directory_target(target)


def _continued_weather_location(previous, configured_location):
    weather = previous.get("weather") or {}
    if not weather.get("city") or not weather.get("date"):
        return None
    kind = weather.get("location_kind")
    if kind not in {"city", "district", "coordinates", "configured"}:
        return None
    point = {"name": weather["city"], "kind": kind, "latitude": weather.get("latitude"),
             "longitude": weather.get("longitude"), "parent": weather.get("parent")}
    if type(point["latitude"]) not in (int, float) or not -90 <= point["latitude"] <= 90:
        return None
    if type(point["longitude"]) not in (int, float) or not -180 <= point["longitude"] <= 180:
        return None
    if kind == "configured" and (not configured_location or any(
            configured_location.get(key) != point[key] for key in ("name", "latitude", "longitude"))):
        return None
    return point


def continue_calendar(text, previous, *, configured_location=None):
    followup = calendar_followup(text, previous)
    if not followup:
        return None, None
    calendar = followup["intent"]
    if followup["explicit"] or calendar["action"] != "calendar_list":
        return calendar, None
    kinds = [row["kind"] for row in previous.get("local_overview", ())]
    alternatives = [topic for kind, topic in (("weather", "天气"), ("reminders", "提醒")) if kind in kinds]
    if not alternatives:
        return calendar, None
    day = followup["day"]
    location = _continued_weather_location(previous, configured_location) if (
        day in WEATHER_DAYS and kinds.count("weather") == kinds.count("calendar") == 1 and alternatives == ["天气"]) else None
    if location:
        weather = previous["weather"]
        readings = {
            "calendar": {"kind": "calendar", "intent": calendar},
            "weather": {"kind": "weather", "place": "", "period": WEATHER_DAYS[day],
                        "part_of_day": followup["part"] or weather.get("part_of_day"), "focus": weather.get("focus") or []},
        }
        return None, {"action": "local_overview", "queries": [readings[kind] for kind in kinds if kind in readings],
                      "weather_location": location}
    queries = "、".join(["日程", *alternatives])
    return {"action": "calendar_invalid",
            "detail": f"上一轮同时查看了{queries}。请说明要继续查哪一项，例如“明天下午的日程”。"}, None


def directory_target(target, *, allow_media_folder=False):
    alias = re.fullmatch(r"(?:一下)?(?:我的?)?(桌面|下载|文档|图片|音乐|视频)(?:目录|文件夹)?", target)
    if alias:
        if not allow_media_folder and alias[1] in {"图片", "音乐", "视频"} and target == alias[1]:
            return None
        return {"action": "directory", "folder": alias[1]}
    target = re.sub(r"^(?:文件夹|目录)\s*[:：]?\s*", "", target).strip().strip('"“”`').strip()
    drive = re.fullmatch(r"([a-zA-Z])\s*盘\s*(?:根目录|(?:的\s*)?[\\/]?(.*))?", target)
    if drive:
        target = f"{drive[1].upper()}:\\" + (drive[2] or "")
    if re.match(r"^[a-zA-Z]:[\\/]|^\\\\(?!\.{1,2}\\)[^\\/:]+\\[^\\/:]+", target) and not re.search(r'[\x00-\x1f"<>|?*]', target):
        return {"action": "directory", "path": target}
    if target.startswith("\\\\") or re.match(r"^[a-zA-Z]:[\\/]", target):
        return {"action": "directory", "invalid": True}
    return None


def parse_file_plan(request, previous, page):
    command, had_preview = PLAN_SUFFIX.subn("", request)
    command = command.strip()
    match = (re.fullmatch(rf"{BY_FILE_TYPE}\s*{ORGANIZE_FILES}\s*(.*)", command)
             or re.fullmatch(rf"(?:(?:把|将)\s*)?(.+?)\s*{BY_FILE_TYPE}\s*{ORGANIZE_FILES}", command)
             or re.fullmatch(r"(?:给|为)\s*(.+?)\s*(?:做|生成|列出)(?:一份|一个)?(?:文件)?(?:整理|分类)(?:计划|方案|预览)", command))
    if not match and had_preview:
        match = re.fullmatch(rf"{ORGANIZE_FILES}\s*(.*)", command)
    if not match:
        return None
    target = match[1].strip()
    if target and not re.fullmatch(r"(?:这个|当前|上一个|该|这|本)(?:目录|文件夹)(?:里的文件)?|这里", target):
        spoken_drive = re.sub(r"^([a-zA-Z]\s*盘)里(?:面)?的?\s*", r"\1的", target)
        location = directory_target(spoken_drive, allow_media_folder=True)
        if location:
            return {**location, "action":"file_plan"}
        if re.search(r"^(?:[a-zA-Z]:|[\\/]|\.{1,2}[\\/])|(?:目录|文件夹)$", target.strip('"“”`')):
            return {"action":"file_plan", "invalid":True}
        return None
    focus = previous.get("directory_focus") if previous and previous.get("status") == "replied" and previous.get("source") == "context" else None
    if not focus and page:
        focus = page.get("focus")
        if not focus and "identity" in page:
            focus = {"path":page["path"], "identity":page["identity"][:3]}
    return {"action":"file_plan", "focus":focus}


def confirm_file_plan(intent):
    plan = intent["plan"]
    if not plan or plan.get("status") != "preview":
        return "请先查看整理预览，再点名要归类的文件。", {}
    name = intent["name"]
    matches = [row for row in plan["items"] if row["name"].casefold() == name.casefold()]
    if len(matches) != 1:
        return "请使用整理预览里显示的一份文件的完整名称。", {}
    row = matches[0]
    if row["status"] == "conflict":
        return "这份文件的分类位置有冲突。处理冲突后请重新查看预览。", {}
    if not (snapshot := intent["snapshots"].get(row["name"])):
        return "这份文件目前只能查看分类建议。请在本机固定磁盘重新查看预览后再试。", {}
    try:
        current = snapshot_file(Path(snapshot["root"]), row["name"], row["category"])
    except ValueError as exc:
        return str(exc), {}
    if current != snapshot:
        return "文件或分类位置在预览后发生了变化，请重新查看整理预览。", {}
    source = Path(snapshot["root"]) / row["name"]
    destination = source.parent / row["category"] / row["name"]
    confirmation = {"action":"organize_file", "title":"归类一份文件",
                    "detail":f"原位置  {source}\n归类到  {destination}", "snapshot":snapshot}
    return None, {"desktop_confirmation":confirmation, "directory_focus":intent["focus"]}


async def run_context(intent, *, timers=None, chat_id=None, calendar_read=None, on_progress=None, profile=None, mcp_selector=None):
    action = intent["action"]
    if action == "mcp_media":
        return await run_media_mcp(intent), {}
    if action == "mcp_configured":
        metadata = {}
        reply = await run_configured_mcp(intent, metadata, select=mcp_selector)
        return reply, metadata
    if action == "device_status":
        return await device_status(intent), {}
    if action == "system_media":
        return await run_system_media(intent["operation"], target=intent.get("target"))
    if action == "directory":
        page = intent.get("page")
        if intent.get("invalid") or ("page" in intent and not (page and page["more"])):
            reply, page = list_directory(intent)
            return reply, {"directory_page": page}
        return await read_directory(intent, profile)
    if action == "file_plan":
        if intent.get("invalid"):
            return "请提供完整的盘符目录或共享目录，例如 D:\\资料。", {}
        focus = intent.get("focus")
        if "focus" in intent and not focus:
            return "请先列出要整理的目录，或直接提供目录路径。", {}
        return await read_directory(intent, profile)
    if action == "file_plan_apply":
        return await asyncio.to_thread(confirm_file_plan, intent)
    if action == "local_overview":
        windows = []

        def read_calendar(query):
            text = calendar_read(query)
            windows.append(calendar_list_window(query))
            return text

        reply, metadata = await run_local_overview(intent, timers=timers, chat_id=chat_id,
                                                  calendar_read=read_calendar if calendar_read else None,
                                                  on_progress=on_progress)
        if windows:
            metadata["_calendar_list_windows"] = windows
        return reply, metadata
    prompts = {"clipboard": "点击读取剪贴板，文字只在此页显示。", "screenshot": "选择要截图的窗口，可预览并保存图片。",
               "screen_image": "选择一个窗口或标签页，预览后可以就画面提问。",
               "screen_text": "选择一个窗口，我会在本机识别画面上的文字。"}
    return prompts[action], {"context_action": action}


def validate_png(data):
    if not data or len(data) > MAX_CAPTURE_BYTES:
        raise ValueError("截图须为 8 MiB 以内的 PNG。")
    try:
        with Image.open(io.BytesIO(data), formats=("PNG",)) as image:
            if max(image.size) > 4096 or image.width * image.height > MAX_PIXELS:
                raise ValueError("截图尺寸超出限制，请选择较小的窗口。")
            image.verify()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise ValueError("无法解码这张 PNG 截图。") from exc


async def recognize_capture(data):
    await asyncio.to_thread(validate_png, data)
    shell_path = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = Path(__file__).with_name("screen_ocr.ps1")
    async with OCR_LIMIT:
        proc = await asyncio.create_subprocess_exec(
            str(shell_path), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(command),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            async with asyncio.timeout(25):
                output, error = await proc.communicate(data)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
    if proc.returncode:
        if b"MOSS_OCR_LANGUAGE" in error:
            raise RuntimeError("请在 Windows 语言设置中安装文字识别语言包。")
        raise RuntimeError("本机 Windows 文字识别未能处理这张图片。")
    return re.sub(r"(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff])", "", output.decode("utf-8-sig").strip())
