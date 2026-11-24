"""Resolve explicit Jev draft handoffs against an opt-in local contact book."""

import hashlib
import json
from pathlib import Path
import re
import unicodedata
from uuid import UUID


CONTACT_FILE = "jev-contacts.json"
INVALID_BOOK = "本机联系人文件格式有误，请检查联系人编号、名称和别名。"
HANDOFF = re.compile(
    r"(?:(?:(?:请|麻烦)\s*(?:你\s*)?(?:帮我)?|帮我)\s*[,，]?\s*)?"
    r"(?:把|将)\s*(?:这份(?:回复)?草稿|刚才(?:的拟回复|生成的回复)|这条回复(?:草稿)?)\s*交给\s*([^\r\n]+)"
)
QUOTES = {"“": "”", "「": "」", "『": "』", '"': '"'}
QUOTE_MARKS = frozenset(QUOTES) | frozenset(QUOTES.values())
POLITE_END = re.compile(r"(?:可以吗|好吗|吧)\s*\Z")
OTHER_ACTION = re.compile(
    r"不是|不要|不用|不必|不准|不能|不可以|不交给|别(?:发|交|给|转)|以外|除外|"
    r"发送|发给|发出|发过去|直接发|转发|转交|发微信|发邮件|发消息|然后|顺便|并且"
)


def _match_key(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def handoff_alias(text: str) -> str | None:
    """Return the named recipient only for an explicit draft-handoff command."""
    if not isinstance(text, str):
        return None
    command = text.strip().rstrip("。.!！?？").rstrip()
    if len(command.splitlines()) > 1:
        return None
    match = HANDOFF.fullmatch(command)
    if not match:
        return None
    target = match[1].strip()
    if not target:
        return None
    quoted = target[0] in QUOTES
    if quoted:
        end = target.find(QUOTES[target[0]], 1)
        tail = target[end + 1:].strip()
        if end < 0 or (tail and not POLITE_END.fullmatch(tail)):
            return None
        target = target[1:end].strip()
    else:
        target = POLITE_END.sub("", target).rstrip()
    normalized = _match_key(target)
    if not normalized or OTHER_ACTION.search(normalized.replace(" ", "")):
        return None
    if quoted:
        return target if not any(char in QUOTE_MARKS or unicodedata.category(char) in {"Cc", "Cs"}
                                 for char in normalized) else None
    if not all(char in " ·・.’'_-+@" or unicodedata.category(char)[0] in "LMN" for char in normalized):
        return None
    return target


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError(INVALID_BOOK)
    return result


def _label(value):
    if not isinstance(value, str):
        raise ValueError(INVALID_BOOK)
    label = " ".join(unicodedata.normalize("NFC", value).split())
    if not label or any(unicodedata.category(char) in {"Cc", "Cs"} for char in label):
        raise ValueError(INVALID_BOOK)
    return label


def match_contact(profile: Path, alias: str) -> dict[str, str]:
    """Resolve a name or alias exactly and version only the matching contact."""
    if not isinstance(alias, str) or not (key := _match_key(alias)):
        raise ValueError("请提供这份草稿的联系人姓名或别名。")
    try:
        book = json.loads((profile / CONTACT_FILE).read_text(encoding="utf-8-sig"),
                          object_pairs_hook=_unique_object)
    except FileNotFoundError:
        raise ValueError("尚未配置本机联系人。") from None
    except OSError:
        raise ValueError("暂时无法读取本机联系人文件。") from None
    except (ValueError, RecursionError):
        raise ValueError(INVALID_BOOK) from None
    if not isinstance(book, dict) or set(book) != {"contacts"} or not isinstance(book["contacts"], list):
        raise ValueError(INVALID_BOOK)

    known_ids = set()
    matches = []
    for contact in book["contacts"]:
        if not isinstance(contact, dict) or set(contact) != {"id", "display_name", "aliases"}:
            raise ValueError(INVALID_BOOK)
        contact_id = contact["id"]
        try:
            valid_id = isinstance(contact_id, str) and str(UUID(contact_id)) == contact_id
        except ValueError:
            valid_id = False
        if not valid_id or contact_id in known_ids or not isinstance(contact["aliases"], list):
            raise ValueError(INVALID_BOOK)
        known_ids.add(contact_id)
        display_name = _label(contact["display_name"])
        aliases = sorted({_match_key(_label(value)) for value in contact["aliases"]})
        if key == _match_key(display_name) or key in aliases:
            payload = json.dumps([contact_id, display_name, aliases], ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            matches.append({"id": contact_id, "display_name": display_name, "digest": digest})

    if not matches:
        raise ValueError("没有找到这个本机联系人，请核对姓名或别名。")
    if len(matches) != 1:
        raise ValueError("这个联系人称呼有重名，请使用地址簿中的唯一别名。")
    return matches[0]
