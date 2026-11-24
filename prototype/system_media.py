import asyncio
import json
import os
import re
import subprocess
from pathlib import Path


_TARGET = r"(?:系统(?:媒体会话|音乐播放器|媒体播放器|播放器)|windows(?:媒体会话|媒体播放器|播放器)|外部播放器)"
_VERBS = {
    "pause": r"(?:暂停播放|暂停)",
    "resume": r"(?:继续播放|恢复播放|继续|恢复)",
    "next": r"(?:播放下一首|切换到下一首|切到下一首|切下一首|下一首)",
    "previous": r"(?:播放上一首|切换到上一首|切到上一首|切上一首|上一首)",
}
_PAUSED_PLAYER = rf"(?:刚才(?:暂停(?:播放)?|停下)的(?:那个|那台)?|刚才的?(?:那个|那台)){_TARGET}"
_RESUME_PAUSED = rf"(?:{_VERBS['resume']}|继续放|接着播放|接着放)"
_STATUS = r"(?:现在|当前)?(?:正在|在)?(?:播放|放|播)?(?:的)?(?:是)?(?:什么(?:音乐|歌曲|歌|曲目)?|哪(?:一)?首(?:歌曲|歌)?)(?:名字)?"
_STATE = {"Playing": "正在播放", "Paused": "已暂停", "Stopped": "已停止", "Changing": "正在切换", "Opened": "已打开", "Closed": "已关闭"}
_LABEL = {"pause": "暂停", "resume": "继续播放", "next": "下一首", "previous": "上一首"}


def system_media_intent(text: str, *, explicit_status: bool = False) -> str | None:
    query = re.sub(r"[\s，,]+", "", text.casefold()).strip("。.!！?？")
    query = re.sub(r"^(?:(?:麻烦|请)(?:你)?|帮我|给我|能不能|能否)+", "", query)
    query = re.sub(r"(?:一下|好吗|可以吗|好么|吧|呢|呀|啊)+$", "", query)
    if re.fullmatch(rf"{_RESUME_PAUSED}{_PAUSED_PLAYER}|(?:让|把|将)?{_PAUSED_PLAYER}{_RESUME_PAUSED}", query):
        return "resume_paused"
    optional_detail = "" if explicit_status else "?"
    if re.fullmatch(
        rf"(?:看看|查看|查询|告诉我|查一下|看一下)?(?:现在|当前)?{_TARGET}(?:里|中)?"
        rf"(?:{_STATUS}|(?:当前的?|现在的?)?(?:曲目|歌曲|播放状态)|的(?:当前曲目|播放状态)){optional_detail}"
        rf"|(?:查询|查看|看看)?(?:当前|现在)?(?:正在)?(?:播放什么(?:歌)?|播放状态)(?:的)?{_TARGET}",
        query,
    ):
        return "status"
    for action, verb in _VERBS.items():
        if re.fullmatch(rf"(?:让|把|将)?{_TARGET}(?:的播放)?{verb}|{verb}(?:一下)?{_TARGET}(?:的播放)?", query):
            return action
    return None


async def query_system_media(action: str, *, expected_source: str = "", expected_title: str = "",
                             paused_target: dict | None = None) -> dict:
    if action not in {"inventory", "status", *_VERBS}:
        raise ValueError("未知的系统媒体动作")
    if os.name != "nt":
        return {"code": "unavailable"}
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = Path(__file__).with_name("system-media.ps1")
    env = {**os.environ, "MOSS_MEDIA_EXPECT_SOURCE": expected_source, "MOSS_MEDIA_EXPECT_TITLE": expected_title,
           "MOSS_MEDIA_RESUME_TARGET": json.dumps(paused_target, ensure_ascii=False) if paused_target else ""}
    try:
        process = await asyncio.create_subprocess_exec(
            str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-Action", action,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=env, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError:
        return {"code": "unavailable"}
    try:
        raw, _ = await asyncio.wait_for(process.communicate(), timeout=8)
        result = json.loads(raw.decode("utf-8-sig"))
        if process.returncode == 0 and isinstance(result, dict) and isinstance(result.get("code"), str):
            return result
    except (ValueError, UnicodeError, TimeoutError):
        pass
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    return {"code": "unavailable"}


def system_media_text(action: str, result: dict) -> str:
    code = result.get("code")
    if code == "none":
        return "Windows 当前没有可用的系统媒体会话。"
    if code == "ambiguous":
        return "Windows 有多个媒体会话，尚未指定当前播放器。"
    if code == "target_missing":
        return "Windows 当前没有提供与刚才记录相符的播放器和曲目。"
    if code == "target_ambiguous":
        return "有多个播放器会话符合刚才的记录，无法确认要继续哪一个。"
    if code == "target_inactive":
        return "刚才的播放器已离开暂停状态，当前无法续播。"
    if code == "changed":
        return "当前媒体会话已变化，指令已取消。"
    if code == "unavailable":
        return "暂时无法读取 Windows 系统媒体会话。"
    info = result.get("after") or result.get("before") or {}
    title = info.get("title", "").strip()
    artist = info.get("artist", "").strip()
    track = ""
    if title:
        track = (artist + "的" if artist else "") + f"《{title}》"
    state = _STATE.get(info.get("state"), "状态未知")
    label = _LABEL.get(action, "播放")
    if code == "status":
        return f"系统播放器{state}，当前曲目是{track}。" if track else f"系统播放器{state}，尚未提供曲名。"
    if code == "already":
        if action == "resume_paused" and track:
            return f"{track}正在播放。"
        return f"系统播放器{state}。"
    if code == "unsupported":
        return f"当前系统播放器不支持“{label}”。"
    if code == "rejected":
        return f"当前系统播放器拒绝了“{label}”指令。"
    if code == "accepted":
        return f"系统播放器已接收“{label}”指令，尚未回报变化。"
    if code == "observed":
        if action == "resume_paused" and track:
            return f"已继续播放{track}。"
        if action in {"next", "previous"}:
            return f"已切到{label}，当前曲目是{track}。" if track else f"已切到{label}。"
        return f"系统播放器{state}。"
    return "没有收到可确认的系统播放器回执。"


def paused_media_target(result: dict) -> dict | None:
    if result.get("code") not in {"observed", "already"}:
        return None
    state = result.get("after") or result.get("before") or {}
    if (state.get("state") != "Paused" or not isinstance(state.get("source"), str) or not state["source"]
            or not isinstance(state.get("title"), str) or not state["title"]):
        return None
    if not isinstance(state.get("artist"), str) or type(state.get("track")) is not int:
        return None
    return {key: state[key] for key in ("source", "title", "artist", "track")}


async def run_system_media(action: str, *, target: dict | None = None) -> tuple[str, dict]:
    if action == "resume_paused":
        if not target:
            return "这段对话里没有可确认续播的外部播放器。", {}
        result = await query_system_media("resume", paused_target=target)
        return system_media_text(action, result), {}
    result = await query_system_media(action)
    details = {"system_media_pause": paused_media_target(result)} if action == "pause" else {}
    return system_media_text(action, result), details
