import re
import unicodedata


_REPLIES = {
    "在吗": "我在。",
    "你在吗": "我在。",
    "在不在": "我在。",
    "哎": "我在。",
    "唉": "我在。",
    "谢谢": "不客气。",
    "谢谢你": "不客气。",
    "多谢": "不客气。",
    "算了": "好，先到这里。",
}
_GREETINGS = {
    "你好": "你好。", "您好": "您好。",
    "早": "早。", "早安": "早安。", "早上好": "早上好。",
    "中午好": "中午好。", "下午好": "下午好。", "晚上好": "晚上好。", "晚安": "晚安。",
}
_GREETING = re.compile(
    r"(?:(?:moss|莫斯|摩斯|墨斯)[\s,、:。!]*)?"
    r"(" + "|".join(_GREETINGS) + r")(?:啊|呀)?"
    r"(?:[\s,、:。!]*(?:moss|莫斯|摩斯|墨斯))?"
)
_REACTION = re.compile(
    r"(?:那就[\s,]*)?"
    r"(?:(?:请(?:你|您)?|麻烦(?:你|您)?|帮我|给我|(?:你|您)?(?:能不能|能否|可以|能))[\s,]*){0,3}"
    r"(?:轻轻(?:地)?\s*)?(?:(?P<laugh>笑一?(?:声|下))|叹一?口气)"
    r"(?:\s*(?:吧|吗|好吗|可以吗))?"
)


def _trim_phrase(text):
    return unicodedata.normalize("NFKC", text).strip().casefold().rstrip("。!?.…").rstrip()


def _normalized_phrase(text):
    phrase = _trim_phrase(text)
    if phrase.startswith("moss"):
        phrase = phrase[4:].lstrip(" ,")
    return phrase


def _reaction_text(phrase):
    if match := _REACTION.fullmatch(phrase):
        return "哈哈。" if match["laugh"] else "唉。"
    return None


def reaction_reply(text):
    return _reaction_text(_normalized_phrase(text))


def greeting_reply(text):
    match = _GREETING.fullmatch(_trim_phrase(text))
    return _GREETINGS[match[1]] if match else None


def everyday_reply(text):
    phrase = _normalized_phrase(text)
    return _REPLIES.get(phrase) or greeting_reply(text) or _reaction_text(phrase)


_READ_REFERENCE = r"(?:刚才[那这][段句首阕](?:话|诗|词)?|上一[个条段句](?:回复|回答|话)?|原文|原话)"
_READ_AGAIN = re.compile(
    r"(?:(?:你|您)?(?:能不能|可不可以|可以|能)|你|您)?(?:请[你您]?|麻烦[你您]?|帮我|给我)?"
    rf"(?:把?{_READ_REFERENCE})?"
    r"(?:(?:再|重新|重)?(?:给我)?(?:朗读|朗诵|吟诵?|诵读|念|读|背诵?)一(?:遍|回|次)"
    r"|(?:再|重新|重)(?:给我)?(?:说|讲)一(?:遍|回|次)"
    r"|(?:再|重新)?(?:给我)?重复一(?:遍|回|次|下))(?:吧|好吗|可以吗|行吗|吗)?"
)
_READ_NOT_HEARD = re.compile(
    rf"(?:{_READ_REFERENCE}[,、\s]*)?(?:刚才我?|我(?:刚才)?)?没(?:有)?听(?:清楚?|到|见)"
)
_READ_TONE = re.compile(
    r"(?:请|用|再|更|稍微|稍|语气|声音|读得|念得)*(?:轻柔|温柔|柔和|轻声|舒缓|明快|豪迈|有气势|慢|快)"
    r"(?:一?点儿?|一?些)?(?:地|的(?:语气|声音))?"
)
_READ_BODY_REFERENCE = r"(?:(?:以下|下面)的?(?:这段)?|这段)(?:文字|内容|正文|话)?"
_READ_SCRIPT = re.compile(
    r"(?:(?:请(?:你|您)?|帮我|麻烦(?:你|您)?)[\s，,]*)*"
    r"(?:(?P<direct>朗读|朗诵|念|读出|读)(?:一下)?" + _READ_BODY_REFERENCE
    + r"|把" + _READ_BODY_REFERENCE + r"(?P<read_to_me>朗读|朗诵|念|读)给我听)"
    + r"\s*[：:]\s*(?P<body>.*?)\s*$",
    re.S,
)
_PAUSE = re.compile(r"\[pause(?:\s+(\d{1,5}(?:\.\d{1,3})?)\s*(ms|s))?\]", re.I)
_PAUSE_MARKER = re.compile(r"\[pause\b[^\]\r\n]*(?:\]|$)", re.I | re.M)
READBACK_LOCAL_READINGS = {"clock", "volume", "cpu", "memory", "battery", "disk", "uptime", "device", "weather"}


def requests_readback(text):
    phrase = unicodedata.normalize("NFKC", text)
    match = _READ_AGAIN.search(phrase)
    if not match:
        return False
    leading = phrase[:match.start()].strip(" \t\r\n,。.;!?…")
    trailing = phrase[match.end():].strip(" \t\r\n,。.;!?…")
    return (not leading or bool(_READ_TONE.fullmatch(leading) or _READ_NOT_HEARD.fullmatch(leading))) and (
        not trailing or bool(_READ_TONE.fullmatch(trailing)))


def reading_script(text):
    request = _READ_SCRIPT.fullmatch(text.strip())
    if not request:
        return None
    action = request["direct"] or request["read_to_me"]
    body = request["body"]
    cues = []
    end = 0
    for marker in _PAUSE_MARKER.finditer(body):
        pause = _PAUSE.fullmatch(marker[0])
        if not pause:
            raise ValueError("停顿标记请用 [pause]、[pause 500ms] 或 [pause 1.5s]。")
        number, unit = pause.groups()
        gap = round(float(number) * (1000 if unit.lower() == "s" else 1)) if number else 350
        if not 1 <= gap <= 10000:
            raise ValueError("每处停顿须在 1 毫秒到 10 秒之间。")
        if part := body[end:marker.start()].strip():
            cues.append({"text": part})
        if cues and "pause_ms" in cues[-1]:
            cues[-1]["pause_ms"] += gap
        else:
            cues.append({"pause_ms": gap})
        end = marker.end()
    if tail := body[end:].strip():
        cues.append({"text": tail})
    cleaned = _PAUSE.sub("", body).strip()
    if not cleaned:
        raise ValueError("请提供要朗读的正文。")
    if sum(cue.get("pause_ms", 0) for cue in cues) > 20000:
        raise ValueError("这段文字里的停顿合计最多 20 秒。")
    return {"cues": cues, "category": "verse" if action == "朗诵" else "reply", "text": cleaned}


def readback_source(last):
    if not last or last.get("status") != "replied" or not (last.get("reply") or "").strip():
        return None
    if any(last.get(key) for key in ("error", "receipt", "call_id", "media", "media_context_used", "transport")):
        return None
    source, tools = last.get("source"), last.get("holo_tools")
    if not tools and source in {None, "holo", "smalltalk", "readback", "narration"}:
        return last

    if any(last.get(key) for key in ("mcp_request", "mcp_receipt", "desktop_confirmation", "file_move_receipt",
                                     "partial_reply", "local_overview_partial")):
        return None
    weather = last.get("weather")
    verified_forecast = (isinstance(weather, dict) and weather.get("source") == "Open-Meteo" and weather.get("simple") is True
                         and weather.get("city") and weather.get("date")
                         and not last.get("_weather_memory_versions", last.get("_memory_versions")))
    if source == "holo" and tools == ["get_weather"] and verified_forecast:
        return last

    rows = last.get("local_overview")
    if source == "context" and not tools and isinstance(rows, list) and rows and all(
            isinstance(row, dict) and row.get("kind") in READBACK_LOCAL_READINGS
            and (row["kind"] != "weather" or verified_forecast)
            and isinstance(row.get("reply"), str) and row["reply"].strip() for row in rows):
        if last["reply"] == "\n".join(row["reply"] for row in rows):
            return last
    return None
