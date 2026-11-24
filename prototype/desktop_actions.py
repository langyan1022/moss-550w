import asyncio
import ctypes
import os
from pathlib import Path
import re
import stat
import subprocess
from threading import Lock
import unicodedata
from urllib.parse import urlencode, urlsplit
from uuid import uuid4
from colloquial_zh import normalize_colloquial_erhua

import pythoncom
import pywintypes
import win32api
import win32con
import win32file
import win32job
import win32security
from comtypes import CLSCTX_ALL, COMError
from pycaw.constants import EDataFlow, ERole
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
from win32com.client import Dispatch
from win32com.shell import shell, shellcon


_PREFIX = re.compile(r"^(?:(?:请(?!问)你?|帮我|替我|给我|麻烦你?)\s*)+")
_VOLUME_TIME = r"(?:现在|当前|目前|这会儿)"
_VOLUME_AMOUNT = r"(?:多少|多大|百分之(?:多少|几))"
_VOLUME_READ = re.compile(
    r"(?:(?:查看|查询|读一下|看一下|看看|查一下|告诉我)\s*)?"
    rf"(?:{_VOLUME_TIME})?的?"
    r"(?:(?:系统|电脑|主|扬声器|(?:默认|系统)?(?:音频)?输出(?:设备)?)的?)?"
    rf"(?:{_VOLUME_TIME}的?)?(?:音量|声音大小|声音(?=(?:{_VOLUME_TIME})?(?:是|有|为)?多大))"
    rf"(?:{_VOLUME_TIME})?(?:(?:是|有|为)?{_VOLUME_AMOUNT}?|(?:开|调)到(?:了)?{_VOLUME_AMOUNT})"
    r"(?:了)?[啊呀呢吗么]?[？?]?"
)
_DESKTOP_TARGET = r"(?:(?:我(?:的|这台)?|这台)?电脑|系统|本机)"
_AUDIO_NAME = r"的?(?:音量|声音|声儿|音频)"
_MUTE_TARGET = rf"{_DESKTOP_TARGET}(?:{_AUDIO_NAME})?"
_SYSTEM_MUTE = re.compile(
    rf"(?:(?:给|让|把|将)?\s*{_MUTE_TARGET}\s*(?:(?:设为|设置为|切换到)\s*)?静音|静音\s*{_MUTE_TARGET})(?:吧)?"
)
_SYSTEM_UNMUTE = re.compile(
    rf"(?:(?:取消|解除)\s*{_MUTE_TARGET}\s*的?\s*静音|(?:给|让|把|将)?\s*{_MUTE_TARGET}\s*(?:取消|解除)\s*静音)(?:吧)?"
)
_SPEAKER = r"(?:默认(?:音频)?输出(?:设备)?|扬声器)"
_MUTE_READ_TARGET = rf"(?:{_MUTE_TARGET}|{_SPEAKER}(?:{_AUDIO_NAME})?|音量)"
_MUTE_STATE_QUERY = re.compile(
    r"(?:请问|告诉我|查看|查询|查一下|看看|看一下)?"
    rf"{_VOLUME_TIME}?{_MUTE_READ_TARGET}{_VOLUME_TIME}?"
    r"(?:静音(?:了)?(?:吗|么|没(?:有|静音)?)|(?:有没有|是否|是不是|有无)(?:开启|进入)?静音(?:了)?"
    r"|(?:是|处于)静音(?:的|状态)?(?:吗|么)|静不静音|的?静音状态)\??"
)
_RELATIVE_VOLUME = re.compile(
    rf"(?:把|将|让|给)?{_VOLUME_TIME}?(?:{_DESKTOP_TARGET}{_AUDIO_NAME}|{_SPEAKER}(?:{_AUDIO_NAME})?)"
    r"(?:(?:太|有点儿?|有些)(?:大|小)(?:了)?,?)?"
    r"(?:稍微|稍|再|略|往|帮我|给我)*"
    r"(?:(?P<down>调(?:整)?(?:低|小)|降低|降|减(?:小|低)?|低|小)|调(?:整)?(?:高|大)|提高|提升|升(?:高)?|加(?:大|高)?|高|大)"
    r"(?:(?:一?点(?:点|儿)?|一些|些)|(?P<points>[0-9]{1,3}|[零一二两三四五六七八九十百]{1,4})个?百分点)(?:吧|呀)?"
)
_AUDIO_WRITE_LOCK = Lock()
_APP_NAME = re.compile(r"[\w\u3400-\u9fff][\w\u3400-\u9fff .()+&\-]{0,79}")
_OTHER_TARGET = re.compile(r"^(?:第[一二两三四五六七八九十\d]+|这个|那个|刚才|上一|下一|本地|草稿|媒体库|音乐库|视频|电影|片源|文件夹|目录|照片文件|网页|链接|网址)")
_APP_ALIASES = {
    "计算器": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
    "记事本": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App",
    "画图": "Microsoft.Paint_8wekyb3d8bbwe!App",
    "设置": "windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel",
    "系统设置": "windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel",
    "文件管理器": "Microsoft.Windows.Explorer", "文件资源管理器": "Microsoft.Windows.Explorer",
    "资源管理器": "Microsoft.Windows.Explorer", "浏览器": "MSEdge", "edge": "MSEdge",
    "谷歌浏览器": "Google Chrome", "chrome": "Google Chrome",
    "vscode": "Visual Studio Code", "vs code": "Visual Studio Code", "wechat": "微信",
}
_MAINTENANCE = re.compile(r"卸载|反安装|卸除|恢复出厂|uninstall|unins\d*\.exe\b", re.I)
_MEDIA_TERMS = re.compile(r"音频|歌曲|音乐|视频|影片|电影|图片|照片|媒体")
_DIGITS = "零一二三四五六七八九"
CONFIRMED_ACTIONS = {"recycle", "lock", "sleep", "powershell"}
_RECYCLE_ON_DELETE = 0x00080000
_ADD_UNDO_RECORD = 0x20000000


def _percent(value):
    if value.isdecimal():
        return int(value)
    if value in {"一百", "百"}:
        return 100
    value = value.replace("两", "二")
    if len(value) == 1 and value in _DIGITS:
        return _DIGITS.index(value)
    tens = re.fullmatch(r"([一二三四五六七八九]?)十([一二三四五六七八九]?)", value)
    if tens:
        return (_DIGITS.index(tens[1]) if tens[1] else 1) * 10 + (_DIGITS.index(tens[2]) if tens[2] else 0)
    raise ValueError("请使用 0 到 100 之间的音量。")


def parse_desktop(text):
    text = _PREFIX.sub("", text.strip()).strip()
    command = re.fullmatch(r"执行\s*PowerShell\s*[:：]\s*(.+)", text, re.I | re.S)
    if command:
        return {"action":"powershell", "command":command[1]}
    value = text.rstrip("。！!")
    if value in {"锁屏", "锁定电脑", "锁定工作站", "锁屏吧"}:
        return {"action":"lock"}
    if value in {"让电脑睡眠", "让电脑进入睡眠", "电脑进入睡眠", "电脑睡眠", "进入睡眠模式"}:
        return {"action":"sleep"}
    recycle = re.fullmatch(r"(?:把|将)?\s*(?:文件\s*)?[“「\"]?([a-z]:[\\/][^\r\n“”「」\"<>|?*]+?)[”」\"]?\s*"
                           r"(?:移入|移到|放进|放入|送到)\s*(?:Windows\s*)?回收站(?:吧)?", value, re.I)
    if recycle:
        return {"action":"recycle", "path":recycle[1].strip()}
    if _SYSTEM_MUTE.fullmatch(value):
        return {"action": "set_mute", "muted": True}
    if _SYSTEM_UNMUTE.fullmatch(value):
        return {"action": "set_mute", "muted": False}
    audio = "".join(unicodedata.normalize("NFKC", normalize_colloquial_erhua(value)).split())
    if _MUTE_STATE_QUERY.fullmatch(audio):
        return {"action": "get_volume", "mute_state": True}
    if _VOLUME_READ.fullmatch(value):
        return {"action": "get_volume"}
    if relative := _RELATIVE_VOLUME.fullmatch(audio):
        points = _percent(relative["points"]) if relative["points"] else 5
        if not 1 <= points <= 100:
            raise ValueError("单次调整音量请输入 1 到 100 个百分点。")
        return {"action": "adjust_volume", "delta": -points if relative["down"] else points}
    volume = re.fullmatch(r"(?:把|将)?(?:系统|电脑|主)?音量\s*(?:调(?:整)?|设置?|改)(?:到|成|为|至)?\s*(?:百分之)?([0-9０-９]{1,3}|[零一二两三四五六七八九十百]{1,4})\s*[%％]?(?:吧)?", value)
    if volume:
        return {"action": "set_volume", "level": _percent(volume[1])}
    visit = re.fullmatch(r"(?:打开|访问)\s*(?:(?:这个)?(?:网址|网页|网站|链接)\s*[：:]?\s*)?(https?://[^\s<>]+)", value, re.I)
    if visit:
        return {"action": "open_url", "url": visit[1]}
    lookup = re.fullmatch(r"(用浏览器|在浏览器(?:中|里)?)\s*(?:搜索|查询|查找|搜一搜|搜|查)(?:一下|一查)?[：:\s]*(.{1,300})", value)
    if lookup:
        return {"action": "web_search", "query": lookup[2].strip()}
    app = re.fullmatch(r"(?:打开|启动)\s*(?:(?:应用程序|应用|软件)\s*[：:]?\s*)?(.+?)(?:吧)?", value)
    if app:
        name = app[1].strip().strip("《》“”")
        if _APP_NAME.fullmatch(name) and not _OTHER_TARGET.match(name) and not name.endswith(("吗", "么")):
            return {"action": "open_app", "name": name}
    return None


def browser_target(intent):
    if intent["action"] == "web_search":
        query = intent["query"].strip()
        if not query or len(query) > 300:
            raise ValueError("请输入一条不超过 300 字的搜索词。")
        return "https://www.bing.com/search?" + urlencode({"q": query})
    url = intent["url"]
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 or c in '\\<>"' for c in url):
        raise ValueError("这个网址包含无效字符。")
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.port == 0):
        raise ValueError("请提供完整的 HTTP 或 HTTPS 网页地址。")
    return url


def _fold(name):
    return unicodedata.normalize("NFKC", name).casefold()


def resolve_app(name):
    if not _APP_NAME.fullmatch(name) or _MAINTENANCE.search(name):
        raise ValueError("请提供要打开的已安装应用名称。")
    target = _fold(_APP_ALIASES.get(_fold(name), name))
    folder = Dispatch("Shell.Application").NameSpace("shell:AppsFolder")
    matches = []
    for item in folder.Items():
        label, app_id = item.Name, item.Path
        if target in {_fold(label), _fold(app_id)} and not _MAINTENANCE.search(label):
            matches.append((label, app_id))
    if len(matches) != 1:
        if matches:
            raise ValueError(f"Windows 注册了多个名为「{name}」的应用，请使用完整名称。")
        raise ValueError(f"在 Windows 已安装应用中没有找到「{name}」。")
    return matches[0]


def _launch_item(item):
    shell.ShellExecuteEx(fMask=shellcon.SEE_MASK_INVOKEIDLIST | shellcon.SEE_MASK_FLAG_NO_UI,
                         lpIDList=item, nShow=win32con.SW_SHOWNORMAL)


def _recycle_identity(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or ":" in str(path)[2:] or win32file.GetDriveType(path.anchor) != win32con.DRIVE_FIXED:
        raise ValueError("请提供本机固定磁盘上的文件绝对路径。")
    try:
        info = path.lstat()
        for item in (path, *path.parents):
            if item.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("请使用文件的实际位置，回收操作需要直接访问原文件。")
    except OSError as exc:
        raise ValueError("无法访问这份文件，请检查路径和读取权限。") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("请提供一份具体的文件。")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def confirmation(intent, work_dir):
    action = intent["action"]
    if action == "recycle":
        path = str(Path(intent["path"]))
        identity = _recycle_identity(path)
        return {**intent, "path":path, "identity":identity, "title":"移入回收站", "detail":path}
    if action == "lock":
        return {**intent, "title":"锁定 Windows", "detail":"确认后锁定当前电脑。解锁时由你登录 Windows。"}
    if action == "sleep":
        return {**intent, "title":"让电脑进入睡眠", "detail":"确认后 Windows 会尝试进入睡眠。播放和本机工作可能暂时中断。"}
    if action == "powershell":
        folder = str(work_dir)
        return {**intent, "folder":folder, "title":"执行 PowerShell",
                "detail":intent["command"] + "\n\n工作目录 " + folder}
    raise ValueError("未知的确认操作。")


def _recycle_file(intent):
    path = intent["path"]
    if _recycle_identity(path) != intent["identity"]:
        raise ValueError("文件在确认前发生了变化，请重新提出回收请求。")
    operation = pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None, pythoncom.CLSCTX_INPROC_SERVER,
                                            shell.IID_IFileOperation)
    operation.SetOperationFlags(_RECYCLE_ON_DELETE | _ADD_UNDO_RECORD | shellcon.FOF_SILENT |
                                shellcon.FOF_NOERRORUI | shellcon.FOF_NOCONFIRMATION | shellcon.FOFX_EARLYFAILURE)
    target = shell.SHCreateItemFromParsingName(path, None, shell.IID_IShellItem)
    operation.DeleteItem(target, None)
    operation.PerformOperations()
    if operation.GetAnyOperationsAborted() or os.path.lexists(path):
        raise ValueError("Windows 未能把文件移入回收站。")
    return f"已把「{Path(path).name}」移入 Windows 回收站。"


def _sleep_windows():
    # 仅在用户确认本轮睡眠后启用所需权限，并恢复原状态。
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
                                          win32con.TOKEN_ADJUST_PRIVILEGES | win32con.TOKEN_QUERY)
    privilege = win32security.LookupPrivilegeValue(None, win32security.SE_SHUTDOWN_NAME)
    with token:
        previous = win32security.AdjustTokenPrivileges(token, False, [(privilege, win32con.SE_PRIVILEGE_ENABLED)])
        try:
            suspend = ctypes.WinDLL("PowrProf", use_last_error=True).SetSuspendState
            suspend.argtypes = [ctypes.c_ubyte] * 3
            suspend.restype = ctypes.c_ubyte
            if not suspend(False, False, False):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            win32security.AdjustTokenPrivileges(token, False, previous)
    return "Windows 已处理睡眠请求。"


def _default_volume_endpoint():
    enumerator = AudioUtilities.GetDeviceEnumerator()
    device = enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender.value, ERole.eMultimedia.value)
    if not device:
        raise ValueError("Windows 当前没有默认的声音输出设备。")
    return device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)


def _read_default_volume():
    volume = _default_volume_endpoint()
    return round(volume.GetMasterVolumeLevelScalar() * 100), bool(volume.GetMute())


def _volume_and_mute(level, muted):
    return f"系统音量为 {level}%，当前{'已' if muted else '未'}静音。"


def _set_default_mute(muted):
    if type(muted) is not bool:
        raise ValueError("静音状态需要是真或假。")
    volume = _default_volume_endpoint()
    current = bool(volume.GetMute())
    if current != muted:
        volume.SetMute(muted, None)
        current = bool(volume.GetMute())
        if current != muted:
            raise ValueError("Windows 未能核实默认声音输出设备的静音状态。")
    level = round(volume.GetMasterVolumeLevelScalar() * 100)
    return _volume_and_mute(level, current)


def _adjust_default_volume(delta):
    if type(delta) is not int or not 1 <= abs(delta) <= 100:
        raise ValueError("单次调整音量请输入 1 到 100 个百分点。")
    volume = _default_volume_endpoint()
    initial = round(volume.GetMasterVolumeLevelScalar() * 100)
    target = min(100, max(0, initial + delta))
    if target != initial:
        volume.SetMasterVolumeLevelScalar(target / 100, None)
    actual = round(volume.GetMasterVolumeLevelScalar() * 100)
    muted = bool(volume.GetMute())
    if target != initial and (actual - initial) * delta <= 0:
        raise ValueError("音量调整未生效。" + _volume_and_mute(actual, muted))
    return _volume_and_mute(actual, muted)


def _run(intent):
    pythoncom.CoInitialize()
    try:
        action = intent["action"]
        if action == "recycle":
            return _recycle_file(intent)
        if action == "lock":
            if not ctypes.WinDLL("User32", use_last_error=True).LockWorkStation():
                raise ctypes.WinError(ctypes.get_last_error())
            return "Windows 已接受锁屏请求。"
        if action == "sleep":
            return _sleep_windows()
        if action == "open_app":
            label, app_id = resolve_app(intent["name"])
            item, _ = shell.SHParseDisplayName("shell:AppsFolder\\" + app_id, 0)
            _launch_item(item)
            return f"已向 Windows 发出打开「{label}」的请求。"
        if action in {"open_url", "web_search"}:
            url = browser_target(intent)
            os.startfile(url)
            if action == "web_search":
                return f"已将「{intent['query']}」的搜索页交给默认浏览器。"
            return f"已将 {urlsplit(url).hostname} 的网页交给默认浏览器。"
        if action == "set_mute":
            with _AUDIO_WRITE_LOCK:
                return _set_default_mute(intent["muted"])
        if action == "adjust_volume":
            with _AUDIO_WRITE_LOCK:
                return _adjust_default_volume(intent["delta"])
        if action == "get_volume":
            level, muted = _read_default_volume()
            if intent.get("mute_state"):
                return _volume_and_mute(level, muted)
        elif action == "set_volume":
            level = intent["level"]
            if type(level) is not int or not 0 <= level <= 100:
                raise ValueError("音量需要在 0 到 100 之间。")
            with _AUDIO_WRITE_LOCK:
                device = AudioUtilities.GetSpeakers()
                if device is None:
                    raise ValueError("Windows 当前没有默认的声音输出设备。")
                device.volume_percent = level
                level, muted = round(device.volume_percent), bool(device.EndpointVolume.GetMute())
        else:
            raise ValueError("未知的桌面操作。")
        return f"系统音量为 {level}%{'，当前已静音' if muted else ''}。"
    except (OSError, pywintypes.com_error, COMError) as exc:
        raise ValueError("Windows 未能完成这次桌面操作。") from exc
    finally:
        pythoncom.CoUninitialize()


async def run_desktop(intent):
    if intent["action"] == "powershell":
        return await _run_powershell(intent)
    return await asyncio.to_thread(_run, intent)


async def _run_powershell(intent):
    folder = Path(intent["folder"])
    folder.mkdir(exist_ok=True)
    bootstrap = Path(__file__).with_name("desktop-powershell.ps1")
    exe = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    job = win32job.CreateJobObject(None, "MossDesktop-" + uuid4().hex)
    process = None
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        process = await asyncio.create_subprocess_exec(
            str(exe), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(bootstrap),
            cwd=folder, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
        handle = win32api.OpenProcess(access, False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        # 命令在进程加入作业之后才送入，超时可一起结束子进程。
        process.stdin.write(intent["command"].encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        output = bytearray()
        async with asyncio.timeout(30):
            while chunk := await process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > 16384:
                    raise ValueError("PowerShell 输出超过 16 KiB，本轮已结束。")
            code = await process.wait()
        text = output.decode("utf-8", errors="replace").strip()
        if code:
            raise ValueError(f"PowerShell 退出代码 {code}。" + ("\n" + text if text else ""))
        return text or "PowerShell 已执行，命令没有文字输出。"
    except TimeoutError as exc:
        raise ValueError("PowerShell 运行已达到 30 秒，本轮已结束。") from exc
    except OSError as exc:
        raise ValueError("Windows 未能执行本轮 PowerShell 命令。") from exc
    finally:
        job.Close()
        if process:
            process.stdin.close()
            await process.communicate()
