"""Conservative local ASR hotword & Mandarin homophone corrector for MOSS 550W.

Zero external dependencies: loads the 20,900+ Hanzi -> Mandarin Pinyin dictionary
directly from `fui/public/js/pinyin.js` once into memory, and dynamically indexes
canonical artist/title names from `local_media`'s SQLite catalog.

Design rules:
- NEVER alter literal quotes, explicit repeat requests, or stop-word barge-in phrases.
- Apply fixed MOSS/550W/BenBen & high-confidence SenseVoice command homophone repairs.
- Only rewrite media artist/title homophones inside explicit media commands AND ONLY
  when the Mandarin syllable sequence maps UNIQUELY to a canonical entry in the library.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import unicodedata

ROOT = Path(__file__).resolve().parent
PINYIN_JS = ROOT / "fui" / "public" / "js" / "pinyin.js"
DEFAULT_MEDIA_DB = ROOT / "local_media" / "data" / "media.sqlite3"

LITERAL_GUARD = re.compile(r"复述|逐字|原样|一字不差|照念|[“”「」『』\"]")
MEDIA_TRIGGER = re.compile(r"播放|放一?首|来一?首|点一?首歌?|切到|换一?首|听一?(?:首|听|下)|歌单|专辑|的歌")

# Common everyday Chinese phrases that must NEVER be rewritten as an artist/song name
STOP_PHRASES = frozenset({
    "一首", "一下", "一个", "这个", "那个", "今天", "明天", "后天", "昨天", "刚才",
    "音乐", "歌曲", "纯音", "助眠", "好听", "舒服", "放松", "安静", "随机", "随便",
    "现在", "几点", "天气", "提醒", "便签", "音量", "大声", "小声", "暂停", "继续",
    "下一", "上一", "北京", "上海", "深圳", "广州", "播放", "放首", "来首", "点歌",
})

_PINYIN_CHAR_MAP: dict[str, str] | None = None
_MEDIA_CACHE: dict[str, object] = {"stamp": None, "artists": {}, "titles": {}, "exact": set()}


def _load_pinyin_map() -> dict[str, str]:
    global _PINYIN_CHAR_MAP
    if _PINYIN_CHAR_MAP is not None:
        return _PINYIN_CHAR_MAP
    mapping: dict[str, str] = {}
    if PINYIN_JS.is_file():
        src = PINYIN_JS.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'"([A-Z]{1,6})"\s*:\s*"([^"]+)"', src):
            syl, chars = m.group(1), m.group(2)
            for ch in chars:
                if ch not in mapping:
                    mapping[ch] = syl
    _PINYIN_CHAR_MAP = mapping
    return mapping


def hanzi_pinyin_key(text: str) -> tuple[str, ...] | None:
    """Return exact Mandarin syllable tuple for a pure Hanzi span, else None."""
    if not text or not re.fullmatch(r"[\u4e00-\u9fff]{2,8}", text):
        return None
    pmap = _load_pinyin_map()
    out = []
    for ch in text:
        syl = pmap.get(ch)
        if not syl:
            return None
        out.append(syl)
    return tuple(out)


def _index_terms(terms: list[str]) -> dict[tuple[str, ...], str | None]:
    """Map syllable tuple -> canonical Chinese term, or None if ambiguous across >1 terms."""
    bucket: dict[tuple[str, ...], set[str]] = {}
    for raw in terms:
        clean = unicodedata.normalize("NFKC", str(raw or "")).strip()
        # Strip trailing edition parentheticals like "(Live)" or "（现场版）"
        clean = re.sub(r"\s*[（(【\[].*[）)】\]]\s*$", "", clean).strip()
        if not re.fullmatch(r"[\u4e00-\u9fff]{2,6}", clean):
            continue
        if clean in STOP_PHRASES:
            continue
        key = hanzi_pinyin_key(clean)
        if key:
            bucket.setdefault(key, set()).add(clean)
    return {k: (next(iter(v)) if len(v) == 1 else None) for k, v in bucket.items()}


def _load_media_vocab(media_db: Path | None = None):
    db_path = Path(media_db) if media_db else Path(os.environ.get("MOSS_MEDIA_DB") or DEFAULT_MEDIA_DB)
    if not db_path.is_file():
        return {}, {}, set()
    try:
        st = db_path.stat()
        stamp = (str(db_path.resolve()), st.st_mtime_ns, st.st_size)
    except OSError:
        return {}, {}, set()
    if _MEDIA_CACHE["stamp"] == stamp:
        return _MEDIA_CACHE["artists"], _MEDIA_CACHE["titles"], _MEDIA_CACHE["exact"]
    artists_raw: list[str] = []
    titles_raw: list[str] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
        try:
            for (a,) in conn.execute("SELECT DISTINCT artist FROM media WHERE artist != ''"):
                for part in re.split(r"[、,;&/]", str(a or "")):
                    part = part.strip()
                    if part:
                        artists_raw.append(part)
            for (t,) in conn.execute("SELECT DISTINCT title FROM media WHERE kind='audio' AND title != ''"):
                titles_raw.append(str(t or "").strip())
        finally:
            conn.close()
    except sqlite3.Error:
        return {}, {}, set()
    artists_idx = _index_terms(artists_raw)
    titles_idx = _index_terms(titles_raw)
    exact = {v for v in artists_idx.values() if v} | {v for v in titles_idx.values() if v}
    _MEDIA_CACHE.update({"stamp": stamp, "artists": artists_idx, "titles": titles_idx, "exact": exact})
    return artists_idx, titles_idx, exact


def _rewrite_media_spans(text: str, artists_idx: dict, titles_idx: dict, exact: set[str]) -> str:
    """Conservatively repair homophone artist/title spans right after media triggers or before '的'."""
    if not artists_idx and not titles_idx:
        return text

    # 1. Artist before "的" (e.g. "道郎的西海情歌", "屠红刚的精忠报国", "刘得华的冰雨")
    def fix_artist_before_de(m: re.Match) -> str:
        prefix, cand = m.group(1), m.group(2)
        if cand in exact or cand in STOP_PHRASES:
            return m.group(0)
        key = hanzi_pinyin_key(cand)
        canon = artists_idx.get(key) if key else None
        return f"{prefix}{canon}的" if canon else m.group(0)

    out = re.sub(r"(播放|放一?首|来一?首|点一?首歌?|听一?(?:首|听|下)?|换一?首|切到)\s*([\u4e00-\u9fff]{2,4})的", fix_artist_before_de, text)

    # 2. Title right after "<artist>的" or directly after media trigger (2..6 Hanzi)
    def fix_title_after_trigger(m: re.Match) -> str:
        prefix, cand, tail = m.group(1), m.group(2), m.group(3)
        if cand in exact or cand in STOP_PHRASES:
            return m.group(0)
        key = hanzi_pinyin_key(cand)
        if not key:
            return m.group(0)
        canon = titles_idx.get(key) or artists_idx.get(key)
        return f"{prefix}{canon}{tail}" if canon else m.group(0)

    out = re.sub(
        r"(播放|放一?首|来一?首|点一?首歌?|切到|换一?首|的)\s*((?:(?!的)[\u4e00-\u9fff]){2,6})(?=$|[吧呀啊吗呢。！？!?,，\s])()",
        fix_title_after_trigger,
        out,
    )
    return out


def correct_asr_transcript(
    text: str,
    *,
    media_db: Path | None = None,
    known_artists: list[str] | None = None,
    known_titles: list[str] | None = None,
) -> str:
    """Apply conservative MOSS domain hotword & media homophone corrections to raw ASR output."""
    if not isinstance(text, str) or not text.strip():
        return text
    if LITERAL_GUARD.search(text):
        return text

    out = text

    # Pass A: Core MOSS / 550W / BenBen & high-confidence SenseVoice command homophones
    out = re.sub(
        r"(^|(?<=你好)|(?<=嗨)|(?<=嘿)|(?<=喂)|(?<=叫)|(?<=问一下)|(?<=[\s,，]))"
        r"(?:莫斯|磨丝|木丝|默思|摩斯)(?=$|[\s,，。！？!?]|现在|帮|今|明|后|放|播|来|给|查|记|提醒|关|开|切|换|说)",
        r"\1MOSS",
        out,
    )
    out = re.sub(r"五五零\s*[wW]|(?<![A-Za-z0-9])550\s*w(?![A-Za-z0-9])", "550W", out)
    if re.search(r"摸|表情|状态|卖萌|小狗|狗|陪|巡检|小屏", out):
        out = out.replace("奔奔", "笨笨")
    out = re.sub(r"(?:放手|防首)(?=\s*[\u4e00-\u9fffA-Za-z0-9]{1,8}(?:的歌|吧|的|音乐|曲))", "放首", out)
    out = re.sub(r"点手(?=歌)", "点首", out)
    out = re.sub(r"半个[中终]头", "半个钟头", out)
    out = re.sub(r"太势简报|态势减报", "态势简报", out)
    out = out.replace("夜间作梦", "夜间做梦").replace("一剑整理", "一键整理")
    if MEDIA_TRIGGER.search(out):
        out = re.sub(r"都选[西希]", "都选C", out)

    # Pass B: Dynamic media library exact-syllable artist/title homophone correction
    if MEDIA_TRIGGER.search(out):
        if known_artists is not None or known_titles is not None:
            a_idx = _index_terms(known_artists or [])
            t_idx = _index_terms(known_titles or [])
            exact = {v for v in a_idx.values() if v} | {v for v in t_idx.values() if v}
        else:
            a_idx, t_idx, exact = _load_media_vocab(media_db)
        out = _rewrite_media_spans(out, a_idx, t_idx, exact)

    return out
