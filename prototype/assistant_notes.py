import re
from typing import Annotated, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, StringConstraints

from memory_store import now


NoteText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1200)]
PAGE_SIZE = 25
MAX_NOTE_ID = 2**63 - 1
VISIBLE_NOTE_SPEECH = "便签在对话里。"
NOTE_READ_PREFIX = re.compile(r"^(?:(?:请问|请你?|麻烦你?|帮我|给我|能不能|可以)\s*)*")
NOTE_SEARCH = r"(?:(?:查找|查询|搜索|查|找)(?:一下|下)?|找找|查查)"
NOTE_VIEW = rf"(?:{NOTE_SEARCH}|查看|浏览|看看|看一下|看下|看一眼|列一下|列出)"
NOTE_SUBJECT = r"(?:我的?)?便签"
NOTE_DIGITS = "零一二三四五六七八九"
NOTE_ID_TOKEN = r"(?:[+-]?[0-9]+|true|false|零|两|[一二三四五六七八九]|[一二三四五六七八九]?十[一二三四五六七八九]?)"
NOTE_READ = r"(?P<read>读(?:一下|下)?|念(?:一下|下)?|朗读)"
NOTE_PICK = rf"(?:{NOTE_READ}|打开|显示|查看|看看|看一下|看下|看一眼)\s*(?:我的\s*)?"
NUMBERED_NOTE = re.compile(
    NOTE_PICK +
    rf"(?:便签\s*(?P<after>{NOTE_ID_TOKEN})\s*号|(?P<before>{NOTE_ID_TOKEN})\s*号\s*便签)(?:吧|吗)?", re.I
)
ORDINAL_NOTE = re.compile(
    NOTE_PICK +
    rf"(?:便签\s*第\s*(?P<after>{NOTE_ID_TOKEN})\s*(?:条|张|个)|第\s*(?P<before>{NOTE_ID_TOKEN})\s*(?:条|张|个)\s*便签)"
    r"(?:吧|吗)?", re.I
)
BARE_ORDINAL_NOTE = re.compile(rf"(?:{NOTE_READ}\s*)?第\s*(?P<index>{NOTE_ID_TOKEN})\s*条(?:吧|吗)?", re.I)
KEYWORD = r"(?P<keyword>[^，,。！？!?；;\r\n]+?)"
SEARCH_MARKER = r"(?:关于|有关|包含|带有|写着|提到)"
NOTE_QUERIES = (
    rf"{NOTE_VIEW}\s*(?:一条\s*)?{SEARCH_MARKER}\s*{KEYWORD}\s*的\s*{NOTE_SUBJECT}",
    rf"{NOTE_SEARCH}\s*{NOTE_SUBJECT}(?:(?:\s+|[，,]\s*)(?:关于|关键词是)?|"
    rf"(?:里(?:面)?|中)?\s*(?:关于|关键词是|包含|写着|提到))\s*{KEYWORD}",
    rf"(?:在)?{NOTE_SUBJECT}(?:里(?:面)?|中)\s*{NOTE_SEARCH}\s*{KEYWORD}",
    rf"(?:{NOTE_VIEW}\s*)?{NOTE_SUBJECT}(?:里(?:面)?|中)\s*(?:有(?:没有|没)?)(?![没无])\s*"
    rf"(?:{SEARCH_MARKER}\s*)?{KEYWORD}(?:的(?:记录|内容|便签))?(?:吗|么)?",
)
NOTE_FOLLOWING_ACTION = re.compile(
    r"(?:然后|并且|接着|顺便|同时|再|并)\s*(?:请|帮我|给我|替我)?\s*(?:把|将)?\s*"
    r"(?:删除|删掉|清空|归档|恢复|编辑|修改|保存|写入|执行|发送|发给)"
)
NOTE_COMPOUND_OTHER = re.compile(
    r"[，,。；;]\s*(?:顺便|顺带|另外|再帮我|再给我|并帮我|并给我|同时帮我)\s*"
    r"(?:请|帮我|给我|麻烦你?\s*)*"
    r"(?:查|看|算|计算|搜|搜索|读|打开|关|开|播|放|问|告诉|汇总|总结|定|设|提醒)"
)


class NoteWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: NoteText


class NoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int
    action: Literal["edit", "archive", "restore"]
    text: NoteText | None = None


class NoteConflict(ValueError):
    pass


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


def note_number(token):
    if token.isascii():
        if not token.isdigit() or len(token) > 19:
            return None
        value = int(token)
    else:
        tens, separator, ones = token.replace("两", "二").partition("十")
        value = (NOTE_DIGITS.index(tens or "一") * 10 + NOTE_DIGITS.index(ones or "零")
                 if separator else NOTE_DIGITS.index(tens))
    return value if 0 < value <= MAX_NOTE_ID else None


def parse_note(text):
    request = text.strip()
    prefix = r"(?:(?:请|麻烦)\s*)?(?:帮我\s*)?"
    capture = re.fullmatch(prefix + r"(?:加|写|存|留|记)(?:一条|一张|一份|个|个新的)?便签(?:\s*[：:，,]\s*|\s+)(.+)", request, re.S)
    if capture and capture[1].strip(" \t\n。！!？?:：，,"):
        if NOTE_COMPOUND_OTHER.search(capture[1]):
            return None
        return "add", capture[1]
    query = re.fullmatch(prefix + r"(?:查找|查询|搜索|查|找)(?:一下)?(?:我的|一条)?便签\s*[:：]\s*(.+?)\s*", request)
    if query and query[1].strip():
        return "list", query[1]
    if re.fullmatch(prefix + r"(?:(?:打开|看看|查看|列出|看一下)?(?:我的|全部|所有)?便签|我有(?:什么|哪些)便签|我(?:的)?便签(?:写了)?(?:有)?什么)[。！!？?]?", request):
        return "list", ""
    request = NOTE_READ_PREFIX.sub("", request).rstrip("。！？!?").strip()
    normalized = normalize(request)
    if numbered := NUMBERED_NOTE.fullmatch(normalized):
        return ("read" if numbered["read"] else "open"), note_number(numbered["before"] or numbered["after"])
    if ordinal := ORDINAL_NOTE.fullmatch(normalized):
        return ("read_ordinal" if ordinal["read"] else "open_ordinal"), note_number(ordinal["before"] or ordinal["after"])
    if re.fullmatch(rf"{NOTE_VIEW}\s*(?:我的?|全部|所有)?便签(?:吧|吗)?|"
                    rf"{NOTE_SUBJECT}(?:里(?:面)?|中)(?:都)?有(?:什么|哪些)(?:内容|记录)?", request):
        return "list", ""
    for pattern in NOTE_QUERIES:
        if match := re.fullmatch(pattern, request):
            keyword = match["keyword"].strip()
            if keyword and not NOTE_FOLLOWING_ACTION.search(keyword):
                return "list", keyword
    return None


def parse_bare_note_ordinal(text):
    request = NOTE_READ_PREFIX.sub("", text.strip()).rstrip("。！？!?").strip()
    if ordinal := BARE_ORDINAL_NOTE.fullmatch(normalize(request)):
        return ("read_ordinal" if ordinal["read"] else "open_ordinal"), note_number(ordinal["index"])
    return None


class NoteStore:
    def __init__(self, db):
        self.db = db
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS assistant_notes(
                id INTEGER PRIMARY KEY, text TEXT NOT NULL, searchable TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1)""")
            db.execute("CREATE INDEX IF NOT EXISTS notes_by_status ON assistant_notes(archived,id DESC)")

    @staticmethod
    def item(row):
        key, text, created, updated, archived, version = row
        return dict(id=key, text=text, created_at=created, updated_at=updated, archived=bool(archived), version=version)

    def page(self, query="", *, archived=False, before=0, limit=PAGE_SIZE):
        if len(query) > 1200:
            raise ValueError("搜索词不能超过 1200 个字符。")
        rows = self.db.execute(
            """SELECT id,text,created_at,updated_at,archived,version FROM assistant_notes
               WHERE archived=? AND id<? AND instr(searchable,?)>0 ORDER BY id DESC LIMIT ?""",
            (archived, before or 2**63-1, normalize(query.strip()), limit + 1),
        ).fetchall()
        items = [self.item(row) for row in rows[:limit]]
        return dict(items=items, before=items[-1]["id"] if len(rows) > limit else None)

    def add(self, text):
        value = NoteWrite(text=text).text
        timestamp = now()
        cursor = self.db.execute("INSERT INTO assistant_notes(text,searchable,created_at,updated_at) VALUES (?,?,?,?)",
                                 (value, normalize(value), timestamp, timestamp))
        return self.get(cursor.lastrowid)

    def get(self, key):
        if type(key) is not int or not 0 < key <= MAX_NOTE_ID:
            raise ValueError("请使用便签列表里的有效编号。")
        row = self.db.execute("SELECT id,text,created_at,updated_at,archived,version FROM assistant_notes WHERE id=?",
                              (key,)).fetchone()
        if not row:
            raise ValueError("找不到这条便签。")
        return self.item(row)

    def update(self, key, operation):
        current = self.get(key)
        if operation.version != current["version"]:
            raise NoteConflict("这条便签已有新改动，请重新载入再提交。")
        action = operation.action
        if (action == "edit") != (operation.text is not None) or current["archived"] != (action == "restore"):
            raise ValueError("当前便签无法执行该操作。")
        value = operation.text if action == "edit" else current["text"]
        self.db.execute("""UPDATE assistant_notes SET text=?,searchable=?,updated_at=?,
                           archived=?,version=version+1 WHERE id=?""",
                        (value, normalize(value), now(), action == "archive", key))
        return self.get(key)

    def reply(self, command, *, result_ids=None):
        action, value = command
        if action in {"open_ordinal", "read_ordinal"}:
            if result_ids is None:
                return "请先在当前对话搜索或列出便签，再按顺序打开。", None, None
            if not result_ids:
                return "这次搜索没有可打开的便签。", None, None
            if value is None or value > len(result_ids):
                return f"这次便签结果只有 {len(result_ids)} 条。可以换个序号，或重新搜索。", None, None
            action, value = action.removesuffix("_ordinal"), result_ids[value - 1]
        if action == "add":
            note = self.add(value)
            return f"写好了，{note['id']} 号便签。", None, None
        if action in {"open", "read"}:
            try:
                note = self.get(value)
            except ValueError as error:
                return str(error), None, None
            if note["archived"]:
                return f"{value} 号便签已归档，可以在便签窗口恢复。", None, None
            return f"{value} 号便签\n{note['text']}", VISIBLE_NOTE_SPEECH if action == "open" else None, None
        page = self.page(value, limit=5)
        if not page["items"]:
            return ("没有找到匹配的便签。" if value else "你还没有便签。"), None, []
        items = []
        for note in page["items"]:
            first_line = note["text"].splitlines()[0]
            excerpt = first_line[:80]
            if len(note["text"]) > len(excerpt):
                excerpt += "…"
            items.append(f"{note['id']} 号 · {excerpt}")
        if page["before"]:
            items.append("打开便签可以继续查看。")
        return "\n".join(items), VISIBLE_NOTE_SPEECH, [note["id"] for note in page["items"]]
