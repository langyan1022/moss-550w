"""Unified Northern Mandarin erhua (儿化音) & colloquial Chinese normalizer for MOSS parsers.

Used strictly for deterministic intent/routing parsers (weather, clock, timers, calendar,
desktop volume, media, brain topic detection) without altering user-visible chat text or
quoted song/file titles. Safe across all LLM backends (Qwen, DeepSeek, Kimi, Local).
"""

import re


_QUOTED_SPAN = re.compile(r"《[^》\n]*》|“[^”\n]*”|‘[^’\n]*’|「[^」\n]*」|『[^』\n]*』|\"[^\"\n]*\"|`[^`\n]*`")

# Ordered longest/most-specific first so "今儿早" -> "今天早上" before bare "今儿" -> "今天".
COLLOQUIAL_DAY_PATTERNS = (
    (re.compile(r"(?:大后儿个|大后儿)"), "大后天"),
    (re.compile(r"(?:大前儿个|大前儿)"), "大前天"),
    (re.compile(r"(?:后儿个|后个|后儿)一?早(?:儿)?"), "后天早上"),
    (re.compile(r"(?:后儿个|后个|后儿)晚(?:上|儿)?"), "后天晚上"),
    (re.compile(r"(?:明儿个|明个|明儿)一?早(?:儿)?"), "明天早上"),
    (re.compile(r"(?:明儿个|明个|明儿)晚(?:上|儿)?"), "明天晚上"),
    (re.compile(r"(?:今儿个|今个|今儿)一?早(?:儿)?"), "今天早上"),
    (re.compile(r"(?:今儿个|今个|今儿)晚(?:上|儿)?"), "今天晚上"),
    (re.compile(r"(?:昨儿个|昨个|昨儿)晚(?:上|儿)?"), "昨天晚上"),
    (re.compile(r"(?:后儿个|后儿|后个)"), "后天"),
    (re.compile(r"(?:前儿个|前儿|前个)"), "前天"),
    (re.compile(r"(?:明儿个|明儿|明个)"), "明天"),
    (re.compile(r"(?:今儿个|今儿|今个)"), "今天"),
    (re.compile(r"(?:昨儿个|昨儿|昨个)"), "昨天"),
)

# Weather-context colloquial "天儿" ("这天儿冷不冷", "今儿天儿怎么样") -> "天气"
_WEATHER_TIANER = re.compile(
    r"(?:(?<=[今明后昨]天)|(?<=[这那好坏热冷]))天儿|天儿(?=(?:怎么样|如何|好不好|冷不冷|热不热|凉不凉|暖不暖|真冷|真热|不错|挺好|太冷|太热))"
)

# General safe erhua patterns for parser routing (never matches 儿童/女儿/婴儿/儿歌/花儿乐队 etc.)
_SAFE_ERHUA_PATTERNS = (
    # Degree / quantity modifiers ("一点儿", "有点儿", "调小点儿", "来点儿音乐")
    (re.compile(r"(一丁?|有|放|来|听|换|播|选|挑|调|加|减|大|小|高|低|轻|重|快|慢|早|晚|亮|暗|暖|凉|冷|热|厚|薄|多|少|近|远|慢|舒缓|安静|轻柔|温柔)点儿"), r"\1点"),
    # Time duration erhua ("半个钟头儿", "十分钟儿", "五分钟后儿", "一会儿")
    (re.compile(r"钟头儿"), "钟头"),
    (re.compile(r"分钟儿"), "分钟"),
    (re.compile(r"(分钟|小时|钟头|秒钟?)后儿"), r"\1后"),
    # Audio / media nouns ("放首歌儿", "听个曲儿", "电脑声儿") — never touches "儿歌"!
    (re.compile(r"(?<![儿女婴小])歌儿"), "歌"),
    (re.compile(r"(?<![儿女婴小])曲儿"), "曲子"),
    (re.compile(r"(?:电脑|系统|本机|扬声器|电视|喇叭)的?声儿"), lambda m: m.group(0)[:-2] + "声音"),
    (re.compile(r"(?<=[大小高低轻重调关开把将给让])声儿"), "声音"),
    # Common daily erhua nouns ("没事儿", "啥事儿", "哪儿")
    (re.compile(r"事儿"), "事"),
)


def _apply_outside_quotes(text: str, transform) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    if not _QUOTED_SPAN.search(raw):
        return transform(raw)
    out = []
    cursor = 0
    for match in _QUOTED_SPAN.finditer(raw):
        if match.start() > cursor:
            out.append(transform(raw[cursor:match.start()]))
        out.append(match.group(0))
        cursor = match.end()
    if cursor < len(raw):
        out.append(transform(raw[cursor:]))
    return "".join(out)


def _sub_days(segment: str) -> str:
    out = segment
    for pattern, replacement in COLLOQUIAL_DAY_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def normalize_colloquial_days(text: str) -> str:
    """Normalize spoken Chinese day erhua (今儿/明儿/后儿/昨儿/大后儿/明儿早...) outside quotes."""
    return _apply_outside_quotes(text, _sub_days)


def normalize_weather_erhua(text: str) -> str:
    """Normalize day erhua plus weather-specific '天儿' -> '天气' outside quotes."""
    def _fn(seg: str) -> str:
        return _WEATHER_TIANER.sub("天气", _sub_days(seg))

    return _apply_outside_quotes(text, _fn)


def normalize_colloquial_erhua(text: str) -> str:
    """Full safe erhua normalization for deterministic intent parsers outside quotes."""
    def _fn(seg: str) -> str:
        out = _WEATHER_TIANER.sub("天气", _sub_days(seg))
        for pattern, replacement in _SAFE_ERHUA_PATTERNS:
            out = pattern.sub(replacement, out)
        return out

    return _apply_outside_quotes(text, _fn)
