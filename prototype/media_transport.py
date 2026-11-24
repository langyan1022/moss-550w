import math
import re


_VERBS = {"暂停": "pause", "停一下": "pause", "停一停": "pause", "停下": "stop",
          "停止": "stop", "关掉": "stop", "关闭": "stop", "继续": "resume", "恢复": "resume",
          "接着": "resume"}
_VERB = "(?P<verb>" + "|".join(_VERBS) + ")"
_MEDIA = (r"(?:(?:当前(?:的)?|这(?:首|段|个))?(?P<kind>音乐|歌曲|歌|音频|视频|影片|短片|电影|播放器)"
          r"|(?P<this_song>这(?:一)?首))")
_PREFIX = r"\s*(?:(?:MOSS|莫斯|摩斯)\s*[,，、]\s*)?(?:(?:请你?|麻烦你?|帮我|给我|先|那就)\s*)*"
_ENDING = r"(?:一下|一会儿|一会)?(?:吧|啦)?\s*[。！!]?\s*"
_COMMANDS = [
    re.compile(_PREFIX + _VERB + r"(?P<playback>播放|放|播)?" + r"(?:\s*(?:一下|下)?\s*" + _MEDIA + ")?" + _ENDING, re.I),
    re.compile(_PREFIX + r"(?:把|将|让)?" + _MEDIA + r"\s*(?:先)?" + _VERB + r"(?:播放|放|播)?" + _ENDING, re.I),
]
_COLLOQUIAL_STOP = re.compile(
    _PREFIX + r"(?:别|不要|不用)(?:再|继续)?(?:播放|播|放)\s*"
    r"(?:(?P<reference>这(?:一)?首(?:歌曲|歌|曲子)?|这(?:一)?段)(?:了)?|" + _MEDIA + r"了|了)" + _ENDING, re.I
)
_STOP_LISTENING = re.compile(_PREFIX + r"(?:我)?不(?:想)?听了" + _ENDING, re.I)
_VIDEO = {"视频", "影片", "短片", "电影"}
_LABEL = {"pause": "暂停", "resume": "继续播放", "stop": "停止"}


def _target_kind(kind):
    return None if not kind or kind == "播放器" else "video" if kind in _VIDEO else "audio"


def parse_transport(text, live=False, *, stoppable=False, explicit=False):
    for pattern in _COMMANDS:
        match = pattern.fullmatch(text)
        if match:
            verb, kind = match.group("verb", "kind")
            kind = kind or ("歌" if match["this_song"] else None)
            if explicit and not (kind or match.groupdict().get("playback")):
                return None
            if not kind and verb in {"继续", "接着", "恢复"} and "播" not in text and "放" not in text and not live:
                return None
            return _VERBS[verb], _target_kind(kind)
    if stoppable or explicit:
        if match := _COLLOQUIAL_STOP.fullmatch(text):
            reference, kind = match.group("reference", "kind")
            if reference and "首" in reference:
                return "stop", "audio"
            return "stop", _target_kind(kind)
        if not explicit and _STOP_LISTENING.fullmatch(text):
            return "stop", "audio"
    return None


def prepare_transport(action, kind, snapshot, item, player, request_id, queued=None):
    label = _LABEL[action]
    if not player or not request_id:
        return {"reply": f"这次请求没有连接到当前页面的播放器，无法{label}。"}
    if not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", request_id):
        raise ValueError("播放器请求标识无效")
    queue_matches = queued and (not kind or kind == queued["kind"])
    if queue_matches and action == "resume":
        return {"reply": f"《{queued['title']}》已在等待播放。"}
    if queue_matches:
        live = snapshot["state"] in ("playing", "buffering") and (not kind or kind == item.get("kind", "audio"))
        primary = {"turn_id":player["turn_id"], "media_id":player["media_id"], "title":item["title"]} if live else queued
        return {"status": "player_requested", "route_note": "本次播放控制尚未收到页面回执。",
                "transport": {"request_id":request_id, "action":action, "target_turn":primary["turn_id"],
                              "media_id":primary["media_id"], "title":primary["title"], "queued":queued,
                              "control_live":bool(live)}}
    if snapshot["state"] == "idle":
        return {"reply": f"当前页面没有可以{label}的媒体。"}
    if kind and kind != item.get("kind", "audio"):
        target = "视频" if kind == "video" else "音乐"
        return {"reply": f"当前页面没有可以{label}的{target}。"}
    return {"status": "player_requested", "route_note": "本次播放控制尚未收到页面回执。",
            "transport": {"request_id": request_id, "action": action, "target_turn": player["turn_id"],
                          "media_id": player["media_id"], "title": item["title"]}}


def transport_reply(transport, report):
    if not all(report.get(k) == transport[k] for k in ("request_id", "target_turn", "media_id")):
        raise ValueError("播放器回执与本轮控制目标不符")
    action, event = transport["action"], report.get("event")
    if not isinstance(event, str):
        raise ValueError("缺少播放器回执状态")
    label = _LABEL[action]
    title = "《" + transport["title"] + "》"
    queue = transport.get("queued")
    cancelled = report.get("cancelled_queue") is True
    queue_reply = f"已取消待播放的《{queue['title']}》。" if queue and cancelled else ""
    if cancelled and not queue:
        raise ValueError("本轮没有待播放项目")
    if event == "queue_cancelled":
        if not queue_reply or transport.get("control_live"):
            raise ValueError("待播放项目与回执不符")
        return queue_reply
    if event in {"unavailable", "blocked", "failed"}:
        return queue_reply + {"unavailable": f"当前页面的播放对象已经变化，无法{label}{title}。",
                "blocked": f"浏览器阻止了{label}{title}，请点击播放器的播放键。",
                "failed": f"播放器未能{label}{title}。"}[event]
    expected = {"pause": "paused", "resume": "playing", "stop": "stopped"}[action]
    if expected != event or (queue and not (cancelled and transport.get("control_live"))):
        raise ValueError("播放器回执的状态与操作不一致")
    position = report.get("position")
    if type(position) not in (int, float) or not math.isfinite(position) or position < 0:
        raise ValueError("播放器回执缺少有效的播放位置")
    return queue_reply + {"paused": f"已暂停{title}。", "playing": f"正在播放{title}。", "stopped": f"已停止{title}，播放位置已回到起点。"}[event]
