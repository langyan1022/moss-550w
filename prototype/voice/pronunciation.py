"""Local, literal pronunciation substitutions shared by the audition engines."""
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import tempfile
import threading


LEXICON = Path(__file__).with_name("pronunciations.json")
HORIZONTAL_SPACE = r"[^\S\r\n]"
ASCII_WORD = r"(?a:[A-Za-z0-9_])"
_file_lock = threading.Lock()


def _term_form(term):
    term = re.sub(HORIZONTAL_SPACE + "+", " ", term)
    # Treat the engineering style "550 W" and the designation "550W" as one entry.
    return re.sub(r"(?<=[0-9]) (?=[a-zA-Z])", "", term)


def _validate(entries):
    if not isinstance(entries, list):
        raise ValueError("发音词表必须是 JSON 数组。")
    result = []
    seen = set()
    for index, item in enumerate(entries, 1):
        if not isinstance(item, dict) or not {"term", "spoken"} <= set(item) or set(item) - {"term", "spoken", "model"}:
            raise ValueError(f"第 {index} 项需要词语和读音，可指定适用模型。")
        model = item.get("model")
        if model not in (None, "omnivoice", "index", "moss8b", "nano", "zipvoice", "f5", "qwen"):
            raise ValueError(f"第 {index} 项的模型名称无效。")
        values = []
        for field in ("term", "spoken"):
            value = item[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"第 {index} 项的 {field} 不能为空。")
            if any(ord(char) < 32 and char != "\t" for char in value):
                raise ValueError(f"第 {index} 项的 {field} 必须是单行文本。")
            values.append(value.strip())
        term, spoken = values
        key = (model, _term_form(term).casefold())
        if key in seen:
            raise ValueError(f"第 {index} 项与已有词条重复：{term}")
        seen.add(key)
        entry = {"term": term, "spoken": spoken}
        if model:
            entry["model"] = model
        result.append(entry)
    return result


def load_entries():
    """Read the current UTF-8 JSON list, including files saved with a Windows BOM."""
    try:
        with _file_lock:
            content = LEXICON.read_text(encoding="utf-8-sig")
        data = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取发音词表，请检查 UTF-8 和 JSON 格式：{exc}") from exc
    return _validate(data)


def save_entries(entries):
    """Publish the entire validated list atomically, keeping the last valid file on failure."""
    checked = _validate(entries)
    with tempfile.NamedTemporaryFile(dir=LEXICON.parent, suffix=".json", delete=False) as temp:
        staged = Path(temp.name)
    try:
        staged.write_text(json.dumps(checked, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with _file_lock:
            os.replace(staged, LEXICON)
    finally:
        staged.unlink(missing_ok=True)
    return checked


def _literal_pattern(term):
    pieces = []
    for index, char in enumerate(term):
        if char == " ":
            pieces.append(HORIZONTAL_SPACE + "+")
        else:
            if index and term[index - 1] in "0123456789" and char.isascii() and char.isalpha():
                pieces.append(HORIZONTAL_SPACE + "*")
            pieces.append(re.escape(char))
    if re.fullmatch(ASCII_WORD, term[0]):
        pieces.insert(0, "(?<!" + ASCII_WORD + ")")
    if re.fullmatch(ASCII_WORD, term[-1]):
        pieces.append("(?!" + ASCII_WORD + ")")
    return "".join(pieces)


@lru_cache(maxsize=8)
def _compile(rules):
    ordered = sorted(rules, key=lambda item: len(item[0]), reverse=True)
    pattern = "|".join("(" + _literal_pattern(term) + ")" for term, _ in ordered)
    return re.compile(pattern, flags=re.IGNORECASE), tuple(spoken for _, spoken in ordered)


def apply_pronunciations(text, entries=None, *, model=None):
    """Replace longest literal matches once, without rescanning generated speech text."""
    checked = load_entries() if entries is None else _validate(entries)
    active = {}
    for entry in checked:
        scope = entry.get("model")
        if scope is not None and scope != model:
            continue
        term = _term_form(entry["term"])
        key = term.casefold()
        if key not in active or scope is not None:
            active[key] = (term, entry["spoken"])
    if not active:
        return text
    rules = tuple(active.values())
    pattern, spoken = _compile(rules)
    return pattern.sub(lambda match: spoken[match.lastindex - 1], text)
