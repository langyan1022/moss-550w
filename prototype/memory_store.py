from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
import gzip
import json
import math
from pathlib import Path
import re
import unicodedata
from uuid import uuid4


LIMITS = {"context_max_items": 12, "context_max_bytes": 900, "max_item_chars": 160, "max_item_bytes": 360}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_owner_code(owner_code=None):
    norm = str(owner_code or "").strip().upper()
    return norm if norm else "MOSS"


def install_schema(db):
    with db:
        db.execute("BEGIN EXCLUSIVE")
        db.execute("CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY, doc TEXT NOT NULL, pending BLOB)")
        db.execute("CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, owner_code TEXT NOT NULL DEFAULT 'MOSS')")
        chat_columns = {row[1] for row in db.execute("PRAGMA table_info(chats)")}
        if "owner_code" not in chat_columns:
            db.execute("ALTER TABLE chats ADD COLUMN owner_code TEXT NOT NULL DEFAULT 'MOSS'")
        db.execute("CREATE INDEX IF NOT EXISTS chats_by_owner ON chats(owner_code)")
        db.execute("CREATE TABLE IF NOT EXISTS operator_chats(owner_code TEXT PRIMARY KEY, active_chat TEXT REFERENCES chats(id))")
        db.execute("""CREATE TABLE IF NOT EXISTS memories(
                   id TEXT PRIMARY KEY, text TEXT NOT NULL, created_at TEXT NOT NULL,
                   owner_code TEXT NOT NULL DEFAULT 'MOSS', UNIQUE(owner_code, text))""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(memories)")}
        for column in ("topic", "updated_at", "source", "source_chat", "source_turn", "expires_at"):
            if column not in columns:
                db.execute(f"ALTER TABLE memories ADD COLUMN {column} TEXT")
        if "topic" not in columns:
            for key, text in db.execute("SELECT id,text FROM memories").fetchall():
                db.execute("UPDATE memories SET topic=? WHERE id=?", (memory_topic(text), key))
        if "owner_code" not in columns:
            # Rebuild existing memories once so uniqueness is scoped per (owner_code, text) instead of global.
            db.execute("""CREATE TABLE memories_v2(
                       id TEXT PRIMARY KEY, text TEXT NOT NULL, created_at TEXT NOT NULL,
                       topic TEXT, updated_at TEXT, source TEXT, source_chat TEXT,
                       source_turn TEXT, expires_at TEXT,
                       owner_code TEXT NOT NULL DEFAULT 'MOSS', UNIQUE(owner_code, text))""")
            db.execute("""INSERT INTO memories_v2(
                       id,text,created_at,topic,updated_at,source,source_chat,source_turn,expires_at,owner_code)
                       SELECT id,text,created_at,topic,updated_at,source,source_chat,source_turn,expires_at,'MOSS'
                       FROM memories""")
            db.execute("DROP TABLE memories")
            db.execute("ALTER TABLE memories_v2 RENAME TO memories")
        db.execute("CREATE INDEX IF NOT EXISTS memories_by_owner ON memories(owner_code)")
        turn_columns = {row[1] for row in db.execute("PRAGMA table_info(turns)")}
        if "cloud_requests" not in turn_columns:
            db.execute("ALTER TABLE turns ADD COLUMN cloud_requests TEXT")
        if "chat_id" not in turn_columns:
            db.execute("ALTER TABLE turns ADD COLUMN chat_id TEXT REFERENCES chats(id)")
        if "active_chat" not in {row[1] for row in db.execute("PRAGMA table_info(state)")}:
            db.execute("ALTER TABLE state ADD COLUMN active_chat TEXT REFERENCES chats(id)")
        db.execute("INSERT OR IGNORE INTO state(id,revision) VALUES(1,0)")
        chat_id, = db.execute("SELECT active_chat FROM state WHERE id=1").fetchone()
        if chat_id is None:
            chat_id = uuid4().hex
            first = db.execute("SELECT doc FROM turns ORDER BY rowid LIMIT 1").fetchone()
            turn = json.loads(first[0]) if first else {}
            title = chat_title(turn["user"]) if first else "新对话"
            db.execute("INSERT INTO chats(id,title,created_at,owner_code) VALUES(?,?,?,'MOSS')", (chat_id, title, turn.get("created_at") or now()))
            db.execute("UPDATE turns SET chat_id=? WHERE chat_id IS NULL", (chat_id,))
            db.execute("UPDATE state SET active_chat=?, revision=revision+1 WHERE id=1", (chat_id,))
        db.execute("INSERT OR IGNORE INTO operator_chats(owner_code, active_chat) VALUES('MOSS', ?)", (chat_id,))
        db.execute("CREATE INDEX IF NOT EXISTS turns_by_chat ON turns(chat_id)")
        db.execute("""CREATE INDEX IF NOT EXISTS active_turns_by_chat ON turns(chat_id)
                   WHERE json_extract(doc,'$.status') IN ('thinking','pending','saving')""")
        db.execute("""CREATE INDEX IF NOT EXISTS media_turns_by_chat ON turns(chat_id)
                   WHERE json_type(doc,'$.media')='object' AND json_extract(doc,'$.status') NOT IN ('cancelled','error')""")
        db.execute("""CREATE INDEX IF NOT EXISTS playback_by_chat_time ON turns(chat_id,
                   julianday(json_extract(doc,'$.browser_playback.at')) DESC, json_extract(doc,'$.browser_playback.at') DESC)
                   WHERE json_extract(doc,'$.browser_playback.event')='started'""")
    return chat_id


def chat_title(text):
    return " ".join(text.split())[:24] or "新对话"


def memories(db, owner_code=None):
    owner = normalize_owner_code(owner_code)
    fields = ("id", "text", "created_at", "updated_at", "source", "source_chat", "source_turn", "expires_at", "topic")
    rows = db.execute("""SELECT id,text,created_at,COALESCE(updated_at,created_at),COALESCE(source,'legacy'),
                         source_chat,source_turn,expires_at,topic FROM memories
                         WHERE COALESCE(owner_code,'MOSS')=? AND (expires_at IS NULL OR expires_at>?) ORDER BY rowid""",
                      (owner, now()))
    return [dict(zip(fields, row)) for row in rows]


SEAT = re.compile(r"靠窗|窗边|临窗|靠过道|过道边|靠走道|靠通道|(?:过道|走道|通道)(?=的?座位)")
SEAT_NAMES = {"seat.rail": "高铁座位", "seat.air": "飞机座位", "seat": "座位"}
RAIL = re.compile(r"高铁|火车|铁路|动车|列车")
AIR = re.compile(r"飞机|航班|机票|乘机|坐飞机")


def seat_topic(text):
    rail, air = RAIL.search(text), AIR.search(text)
    if rail and air:
        return None
    return "seat.rail" if rail else "seat.air" if air else "seat"


def memory_topic(text):
    if (re.match(r"我的(?:音乐|听歌)偏好|我(?:平时)?听歌(?:最)?(?:偏爱|偏好)", text)
            and music_preference(text)):
        return "preference.音乐"
    if re.search(r"其他人|同事|朋友|家人|妈妈|爸爸|他|她", text):
        return None
    if (len(SEAT.findall(text)) == 1 and re.search(r"我|偏好|喜欢|偏爱|希望", text) and
            (RAIL.search(text) or AIR.search(text) or re.search(r"座位|选座|坐靠", text)) and
            not re.search(r"餐厅|咖啡|房间|酒店|书桌|工位|教室|不|有时|如果|除非|以前|曾经", text) and
            not ("靠窗" in text and re.search(r"过道|走道|通道", text))):
        return seat_topic(text)
    if re.fullmatch(r"我(?:希望你|想让你|要你)(?:以后)?(?:叫|称呼)我(?:为)?[^，。！？?]{1,32}[。！]?", text):
        return "name"
    explicit = re.fullmatch(r"我的([^，。！？?：:]{1,12})偏好(?:是|为|[:：])[^，。！？?]{1,60}[。！]?", text)
    return "preference." + explicit[1] if explicit else None


def music_preference(text):
    value = unicodedata.normalize("NFKC", text).strip().rstrip("。!")
    match = re.fullmatch(
        r"(?:我的(?:音乐|听歌)偏好(?:是|为|:)\s*|我(?:平时)?听歌(?:最)?(?:偏爱|偏好|喜欢))"
        r"([^，,。！？?；;：:\n“”「」《》]{1,40}?)(?:的(?:歌曲|音乐|歌))?",
        value,
    ) or re.fullmatch(
        r"我(?:平时|通常)?(?:最)?(?:喜欢|偏爱|爱)听([^，,。！？?；;：:\n“”「」《》]{1,40}?)的(?:歌曲|音乐|歌)",
        value,
    )
    return match[1].strip() if match else None


def limits(items):
    return {**LIMITS, "used_items": len(items), "used_bytes": sum(len(item["text"].encode("utf-8")) for item in items)}


def search_terms(text):
    words = set()
    text = unicodedata.normalize("NFKC", text).casefold()
    for match in re.finditer(r"[a-z0-9]+|[\u3400-\u9fff]+", text):
        part = match[0]
        if part.isascii() or len(part) == 1:
            words.add(part)
        else:
            words.update(part[i:i + 2] for i in range(len(part) - 1))
    return words


CODE_PART = re.compile(r"(?<![a-z0-9_-])[a-z0-9]+(?:[-_][a-z0-9]+)*(?![a-z0-9_-])")
PERSON_SUBJECT = re.compile(r"^(?:我(?:的)?)?\s*(?:一位|这位|那位|某位)?\s*"
                            r"(朋友|同事|同学|客户(?!机|端|服务器)|访客|家人|父母|孩子|妈妈|爸爸|妻子|丈夫)")
PERSON_QUERY_PREFIX = re.compile(r"^(?:(?:请问|请|麻烦(?:你)?|帮我|给我|告诉我|查(?:询|查|一下)?|看看|"
                                 r"你(?:还)?记得|你知道|我想知道|我记下的|给|替|为)\s*[，,]?\s*)+")
PERSON_PREDICATE = r"(?:最?(?:喜欢|偏爱|爱喝|爱吃)|偏好|通常|平时|住在|叫|喝|吃|想(?:按|根据|放|听))"
PERSON_NAME = re.compile(rf"([\u3400-\u9fff·]{{1,8}}?|[a-z][a-z ]{{0,20}}?)\s*(?:的|(?={PERSON_PREDICATE}))", re.I)
PERSON_ACTIVITY = re.compile(r"^(?:写|发|买|穿|用|选|做)")
SELF_CLAUSE = re.compile(r"(?:^|[，,。；;])\s*我(?:的|平时|通常|习惯|一般)")


def person_subject(text, *, query=False):
    text = unicodedata.normalize("NFKC", text).strip()
    prefix = PERSON_QUERY_PREFIX.match(text) if query else None
    if prefix:
        text = text[prefix.end():]
    match = PERSON_SUBJECT.match(text)
    if not match:
        return None
    details = text[match.end():].lstrip()
    if query and SELF_CLAUSE.search(details):
        return None
    if not query and re.search(r"我(?:的|平时|通常|习惯|一般)", details):
        return None
    if details.startswith("的"):
        return match[1], "", details[1:].lstrip()
    if re.match(PERSON_PREDICATE, details):
        return match[1], "", details
    if name := PERSON_NAME.match(details):
        return match[1], name[1].strip().casefold(), details[name.end():].lstrip()
    recipient = prefix and prefix[0].rstrip("，, \t").endswith(("给", "替", "为"))
    if query and (recipient or PERSON_ACTIVITY.match(details)):
        return match[1], "", details
    return None


def other_person_query(text):
    return person_subject(text, query=True) is not None


OWN_CONTEXT = re.compile(r"我的|我(?:平时|通常|以前|习惯|偏好|喜欢|想|对|记的)|记得|回想|记忆|之前说过|上次")
PERSONAL = re.compile(OWN_CONTEXT.pattern + r"|替我|给我|按我|为我|按.{0,6}(?:偏好|习惯)")
PERSONAL_ADVICE = re.compile(
    r"(?:帮我|给我|为我)(?:推荐|挑(?:选)?|选(?:一个|个)?|搭配|安排|规划|设计)|"
    r"推荐(?:一?个|点|些|几)|适合我|吃什么|喝什么|点什么|穿什么|买什么|"
    r"下午茶|早饭|早餐|午饭|午餐|晚饭|晚餐|夜宵|忌口|过敏|点单|点菜"
)
GENERAL = re.compile(r"为什么|为何|原理|介绍|科普|解释|讲解|知识")
PROPERTY = re.compile(r"的([\u3400-\u9fff]{2,18}?)(?:有多|是多少|叫什么|是什么)")
QUERY_WORDS = re.compile(r"有没有|是什么|哪一种|帮我|给我|替我|按我|我的|我想|我是|我对|什么|怎么|如何|"
                         r"可以|一个|一下|请问|希望|喜欢|偏好|记住|之前|习惯|平时|一般|通常|往常|需要|多少")
FUNCTION_CHARS = set("我你他的她它这那里是有在于就都也又要想用为与吗呢么什哪谁几多有可会了的地得把被给对着过一二三些个种次时后前去来不大好之请帮偏上中下")
GENERIC_TERMS = {"地方", "位置", "东西", "事情", "哪个", "哪儿", "哪里", "颜色", "那个", "这种", "怎样"}
GENERIC_PHRASES = re.compile("|".join(sorted(GENERIC_TERMS)))
SPARSE_STOP = FUNCTION_CHARS | set("选挑找拿乘搭")
UNITS = re.compile(r"(?:多少|几)\s*(毫升|升|毫克|千克|公斤|斤|克|毫米|厘米|公里|千米|米|英寸|寸|元|块钱|码)")
UNIT_FAMILIES = ("毫升|升|ml|l", "毫克|千克|公斤|斤|克|mg|kg|g", "毫米|厘米|公里|千米|米|英寸|寸|mm|cm|km|m", "元|块钱", "码")
NUMBER_VALUE = r"(?:\d+(?:\.\d+)?|[半零一二两三四五六七八九十百千万]+)"
DETAILS = tuple((re.compile(question), re.compile(evidence, re.I)) for question, evidence in (
    (r"(?:什么|啥|哪种|哪个)颜色|是何色", r"颜色(?:是|为)|[红橙黄绿青蓝紫灰白黑粉棕褐金银]色|(?:纯白|纯黑|天蓝|深蓝|墨绿|米白)"),
    (r"星期几|周几|哪天|哪一?日|几月几[号日]", r"(?:周|星期|礼拜)[一二三四五六日天末]|周末|\d{1,2}月\d{1,2}[日号]|\d{4}-\d{2}-\d{2}"),
    (r"几点|什么时刻", NUMBER_VALUE + r"[点时]|\d{1,2}[:：]\d{2}|清晨|早上|上午|中午|下午|傍晚|晚上"),
    (r"(?:单号|编号|序列号|识别码|代号)(?:是|叫|为)?(?:什么|多少|几|哪个)|(?:准确|完整)编号",
     r"(?:编号|单号|序列号|识别码|代号|受理号)|[A-Z]+[-_]?[A-Z0-9]*\d|\d{4,}"),
    (r"谁.{0,3}(送|给|买|写|做|借|卖)|(?:送|给|买|写|借)自谁", r"送|给|买|写|做|借|卖"),
    (r"哪(?:里|儿|家).{0,3}(?:买|购)|(?:买|购).{0,3}哪(?:里|儿|家)", r"买|购|订|下单|网店|门店")
))


def requested_detail(text):
    unit = UNITS.search(text)
    if unit:
        family = next(group for group in UNIT_FAMILIES if unit[1] in group.split("|"))
        return re.compile(NUMBER_VALUE + r"\s*(?:" + family + r")(?![a-z])", re.I)
    return next((evidence for query, evidence in DETAILS if query.search(text)), None)


@lru_cache(maxsize=1)
def noun_lexicon():
    single, senses = {}, {}
    path = Path(__file__).with_name("memory_lexicon") / "common-nouns.tsv.gz"
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for group, line in enumerate(stream):
            for entry in line.rstrip("\n").split("\t"):
                word = entry.removeprefix("*")
                senses.setdefault(word, set()).add(group)
                if entry.startswith("*"):
                    single[word] = group
    return single, senses


def noun_words(text):
    for phrase in re.findall(r"[\u3400-\u9fff]{2,}", QUERY_WORDS.sub(" ", text)):
        for start in range(len(phrase) - 1):
            for size in range(2, min(8, len(phrase) - start) + 1):
                yield phrase[start:start + size]


def synonym_match(question, items):
    single, senses = noun_lexicon()
    groups = {single[w] for w in noun_words(question) if w in single and w not in GENERIC_TERMS}
    if not groups:
        return []
    hits = [i for i, item in enumerate(items)
            if any(groups.intersection(senses.get(w, ())) for w in noun_words(item["text"]))]
    return hits if len(hits) == 1 else []


def identifiers(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = re.sub(r"[‐‑‒–—−]", "-", normalized)
    return [part[0] for part in CODE_PART.finditer(normalized)
            if re.search(r"[a-z]", part[0]) and re.search(r"[0-9]", part[0])]


def general_question(text):
    return bool(GENERAL.search(text) and not OWN_CONTEXT.search(text))


def local_recall_only(text):
    return bool(identifiers(text) or other_person_query(text) or general_question(text)
                or PROPERTY.search(text) or requested_detail(text))


def code_matches(codes, items):
    by_code = {}
    for index, item in enumerate(items):
        for code in set(identifiers(item["text"]) + [item["id"].casefold()]):
            by_code.setdefault(code, []).append(index)
    found = {}
    for code in codes:
        forms = [code] if code in by_code else []
        # 紧凑写法只在库中唯一对应一个编号时展开，保留不同分隔方式可能表达的区别。
        if not forms and "-" not in code and "_" not in code:
            forms = [s for s in by_code if s.replace("-", "").replace("_", "") == code]
        if len(forms) == 1:
            found.update(dict.fromkeys(by_code[forms[0]]))
    return list(found)


def sparse_character_match(query, items):
    def content_chars(text):
        return {c for c in QUERY_WORDS.sub(" ", text) if "\u3400" <= c <= "\u9fff"} - SPARSE_STOP

    wanted = content_chars(GENERIC_PHRASES.sub(" ", query))
    if not wanted:
        return []
    rows = [content_chars(item["text"]) & wanted for item in items]
    counts = Counter(c for row in rows for c in row)
    uncommon = {c for c, count in counts.items() if count <= max(1, len(items) // 5)}
    ranked = sorted(((len(row & uncommon), i) for i, row in enumerate(rows)), reverse=True)
    if ranked and ranked[0][0] >= 2 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        return [ranked[0][1]]
    return []


def select_memories(question, items):
    if general_question(question) or ignore_personal_memory(question):
        return []
    subject = person_subject(question, query=True)
    if subject:
        role, name, details = subject
        matches = [(item, owner[1]) for item in items if (owner := person_subject(item["text"]))
                   and owner[0] == role and (not name or owner[1] == name)]
        if not name and len({stored_name for _, stored_name in matches}) > 1:
            return []
        items = [item for item, _ in matches]
    else:
        details = question
        if SELF_CLAUSE.search(PERSON_QUERY_PREFIX.sub("", question)):
            items = [item for item in items if person_subject(item["text"]) is None]
    codes = identifiers(question)
    if codes:
        return limited_items(items, code_matches(codes, items))
    detail = requested_detail(question)
    if detail:
        items = [item for item in items if detail.search(item["text"])]
    if not items:
        return []
    # “某处的高度是多少”询问的是高度。仅共享地点不能证明这条记忆回答了问题。
    prop = PROPERTY.search(question)
    if prop and not detail and not OWN_CONTEXT.search(question):
        attribute = search_terms(prop[1])
        items = [item for item in items if attribute & search_terms(item["text"])]
    tokens = search_terms(QUERY_WORDS.sub(" ", GENERIC_PHRASES.sub(" ", details)))
    query = {term for term in tokens if not set(term) <= FUNCTION_CHARS}
    matches = [search_terms(item["text"]) & query for item in items]
    if re.search(r"座位|坐哪|选座|靠窗|过道|走道", question) or (
            (RAIL.search(question) or AIR.search(question)) and re.search(r"哪一侧|哪边|选.*位置|挑.*位置", question)):
        topic = seat_topic(question)
        for item, terms in zip(items, matches):
            if item.get("topic") in SEAT_NAMES:
                if item["topic"] in {topic, "seat"} or topic == "seat":
                    terms.add("座位")
                else:
                    terms.clear()
    frequency = Counter(word for terms in matches for word in terms)
    weights = {word: math.log1p((len(items) - count + 0.5) / (count + 0.5)) for word, count in frequency.items()}
    order = sorted((i for i, terms in enumerate(matches) if terms),
                   key=lambda i: (math.fsum(weights[word] for word in matches[i]), i), reverse=True)
    if not order and (subject or PERSONAL.search(question) or question.startswith("我") or detail and "我" in question):
        order = synonym_match(details, items) or sparse_character_match(details, items)
    if not order and not subject and not detail and not prop and PERSONAL_ADVICE.search(question):
        order = [i for i, item in enumerate(items)
                 if person_subject(item["text"]) is None and not identifiers(item["text"])]
    return limited_items(items, order)


def limited_items(items, order):
    chosen = []
    remaining = LIMITS["context_max_bytes"]
    for index in order:
        item = items[index]
        cost = len(item["text"].encode("utf-8"))
        if cost <= remaining:
            chosen.append(item)
            remaining -= cost
            if len(chosen) == LIMITS["context_max_items"]:
                break
    return chosen


def valid_text(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请输入要记住的内容")
    text = text.strip()
    size = len(text.encode("utf-8"))
    if len(text) > LIMITS["max_item_chars"] or size > LIMITS["max_item_bytes"]:
        raise ValueError("每条记忆最多 160 个字符、360 字节")
    return text


def with_expiry(text):
    match = re.search(r"[（(，,；;]\s*(?:只记到|有效至|有效到)\s*([^（）(),，；;]+?)\s*[）)]?[。.]?$", text)
    if not match:
        return text, None
    today = date.today()
    value = match[1]
    try:
        if value in {"今天", "明天"} or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            last = today if value == "今天" else today + timedelta(days=1) if value == "明天" else date.fromisoformat(value)
            cutoff = datetime.combine(last + timedelta(days=1), time.min).astimezone(timezone.utc)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?", value):
            cutoff = datetime.fromisoformat(value).astimezone(timezone.utc)
        else:
            raise ValueError()
        if cutoff <= datetime.now(timezone.utc):
            raise ValueError()
    except (ValueError, OverflowError):
        raise ValueError("有效期可写今天、明天、将来的日期或本地日期时间") from None
    return text[:match.start()].strip(), cutoff.isoformat(timespec="microseconds")


def find_memory(db, target, owner_code=None):
    target = target.strip()
    items = memories(db, owner_code=owner_code)
    exact = [item for item in items if target in (item["id"], item["text"])]
    if not exact:
        aliases = {"座位偏好": "seat", "高铁座位偏好": "seat.rail", "火车座位偏好": "seat.rail",
                   "飞机座位偏好": "seat.air", "称呼": "name"}
        category = aliases.get(target)
        if category:
            exact = [item for item in items if item["topic"] == category or (category == "seat" and item["topic"] in SEAT_NAMES)]
    if len(exact) != 1:
        raise ValueError("有多条匹配的记忆，请指定原文或 ID" if exact else "找不到这条有效记忆，可输入“查看记忆”核对原文或 ID")
    return exact[0]


def save_memory(db, text, *, source, chat=None, turn=None, target=None, owner_code=None):
    owner = normalize_owner_code(owner_code)
    text, expires = with_expiry(valid_text(text))
    text = valid_text(text)
    topic = memory_topic(text)
    old = find_memory(db, target, owner_code=owner) if target else None
    items = memories(db, owner_code=owner)
    same = next((item for item in items if item["text"] == text), None)
    if old is None:
        candidates = [item for item in items if topic and item["topic"] == topic]
        if len(candidates) > 1 and not same:
            raise ValueError("有多条同类记忆，请用“修改记忆：原文 → 新内容”指定要更改的一条")
        old = same or (candidates[0] if candidates else None)
    elif same and same["id"] != old["id"]:
        raise ValueError("已有另一条相同内容的记忆")
    if old and old["text"] == text and (expires is None or expires == old["expires_at"]):
        return old["id"], "unchanged"
    stamp = now()
    db.execute("DELETE FROM memories WHERE COALESCE(owner_code,'MOSS')=? AND text=? AND expires_at<=?", (owner, text, stamp))
    if old:
        db.execute("""UPDATE memories SET text=?,topic=?,updated_at=?,source=?,source_chat=?,source_turn=?,expires_at=?
                      WHERE id=? AND COALESCE(owner_code,'MOSS')=?""",
                   (text, topic, stamp, source, chat, turn, expires or old["expires_at"], old["id"], owner))
        return old["id"], "updated"
    key = uuid4().hex
    db.execute("""INSERT INTO memories(id,text,created_at,updated_at,source,source_chat,source_turn,expires_at,topic,owner_code)
                  VALUES(?,?,?,?,?,?,?,?,?,?)""", (key, text, stamp, stamp, source, chat, turn, expires, topic, owner))
    return key, "created"


def seat_correction(db, change, chat, owner_code=None):
    mode, position = change
    candidates = [item for item in memories(db, owner_code=owner_code)
                  if item["topic"] in SEAT_NAMES and (not mode or item["topic"] == mode)]
    if not mode and len(candidates) > 1:
        scoped = [item for item in candidates if item["source_chat"] == chat]
        if len(scoped) == 1:
            candidates = scoped
    if len(candidates) != 1:
        raise ValueError("请指定要修改的高铁或飞机座位偏好，或使用“修改记忆：原文 → 新内容”")
    memory = candidates[0]
    return memory["id"], SEAT.sub(position, memory["text"], count=1)


def remove_memory(db, key, owner_code=None):
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{32}", key):
        raise ValueError("需要完整的记忆 ID")
    owner = normalize_owner_code(owner_code)
    if db.execute("DELETE FROM memories WHERE id=? AND COALESCE(owner_code,'MOSS')=?", (key, owner)).rowcount != 1:
        raise ValueError("找不到这条记忆")


def ignore_personal_memory(text):
    if re.match(r"^(?:请|帮我)?(?:分析|解释|翻译|改写|润色)(?:一下)?[，,\s]*[“「『\"‘]", text.strip()):
        outside = re.sub(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"]*\"|‘[^’]*’", "", text)
        if outside != text and not OWN_CONTEXT.search(outside):
            return True
    return bool(re.search(r"(?:(?:别|不要|不必|不用|无需)(?:再)?(?:参考|考虑|采用|根据|按照?|用|使用|结合|套用)|不用)"
                          r"(?:我的?|已保存的|之前的)?[\u4e00-\u9fff]{0,8}(?:记忆|偏好|习惯)", text))


CHECK_END = r"(?:了(?:没(?:有)?|[吗嘛么])|没(?:有)?|[吗嘛么]|了?(?=[?？]))[?？。!！]*"
MEMORY_CHECK = re.compile(r"(?:请问[，,\s]*)?(?:请你?|帮我|你|您)?(?:已经)?记住(?!\s*[：:])"
                          r"(?:了(?=[我“「『]))?\s*(.*?)" + CHECK_END)


def check_memory(fact, items):
    def plain(text):
        return unicodedata.normalize("NFC", text).strip().removesuffix("。").rstrip()

    fact = plain(fact)
    quotes = {"“": "”", "「": "」", "『": "』", '"': '"'}
    if len(fact) > 1 and quotes.get(fact[0]) == fact[-1]:
        fact = fact[1:-1].strip()
    matches = [m for m in items if plain(m["text"]) == fact]
    if not matches:
        shortened = re.sub(r"(?:这件事|这一点|这条记忆)$", "", fact).rstrip()
        if shortened != fact:
            fact = shortened
            matches = [m for m in items if plain(m["text"]) == fact]
    if not fact or fact in {"这个", "那个", "这条", "那条", "这些", "那件事", "我说的", "刚才说的"}:
        return [], "请说出想核对的那条内容。"
    if not matches and re.fullmatch(r"我的[\w ]{1,24}", fact) and not re.search(r"是|为|叫|喜欢", fact):
        field = re.compile(re.escape(fact) + r"\s*(?:是|为|叫|[：:])\s*\S")
        matches = [m for m in items if field.match(plain(m["text"]))]
    if len(matches) != 1:
        return [], "没有找到一致的已保存记忆。" if not matches else "请说出想核对的具体内容。"
    item = matches[0]
    return [item["id"]], "已保存的原文是“" + item["text"] + "”。"


RECALL_END = r"(?:来着)?[呢吗么]?[?!.。]*"
RECALL_VALUE = r"(?:(?:是|为|叫)?(?:什么|啥|多少|哪个)|(?:里|中)(?:都)?有(?:什么|啥|哪些))" + RECALL_END
RECALL_LABEL = r"[\w -]{1,32}?"


def saved_recall_request(text):
    query = unicodedata.normalize("NFKC", text).strip()
    query = re.sub(r"^请问[,\s]*", "", query)
    if re.fullmatch(r"我的称呼" + RECALL_VALUE, query) or re.fullmatch(
        r"(?:你还记得)?我(?:之前|上次|先前|刚才)?(?:让|请)你"
        r"(?:叫我(?:什么|啥)|怎么称呼我)" + RECALL_END, query
    ):
        return "name", "称呼"
    if field := re.fullmatch(
        rf"(?:我(?:之前|上次|先前|刚才)?(?:让|请)你|你(?:之前|上次|先前|刚才))"
        rf"(?:记住|记下|记|保存|记录)的(?:我的?)?({RECALL_LABEL})\s*{RECALL_VALUE}", query
    ):
        return "field", field[1].strip()
    if field := re.fullmatch(
        r"我的([\w -]{0,24}?(?:偏好|习惯|工号|编号|单号|代号))\s*" + RECALL_VALUE, query
    ):
        return "field", field[1].strip()
    if re.fullmatch(r"我的阅读清单(?:里|中)(?:都)?有(?:什么|啥|哪些)" + RECALL_END, query):
        return "field", "阅读清单"
    return None


def recall_saved(target, items):
    kind, label = target
    field = re.compile(rf"(?:我的\s*)?{re.escape(label)}\s*(?:是|为|叫|:|=|(?:里|中)?有|包括)\s*\S")
    matches = [
        item for item in items
        if (kind == "name" and item.get("topic") == "name")
        or field.match(unicodedata.normalize("NFKC", item["text"]).strip())
    ]
    if len(matches) != 1:
        return [], "这项有多条记忆，请说一下想查的具体内容。" if matches else "目前没有保存你问的这项信息。"
    item = matches[0]
    return [item["id"]], "已保存的原文是“" + item["text"] + "”。"


def memory_command(text):
    text = text.strip()
    if text in {"列出记忆", "查看记忆", "记忆列表"}:
        return "list", ""
    query = re.fullmatch(r"(?:查询记忆|查记忆)\s*[：:]\s*(.+)", text)
    if query:
        return "recall", query[1]
    seat = re.fullmatch(r"(?:我(?:的|坐|乘)?)(?:高铁|火车|飞机)?(?:的)?座位偏好(?:是什么|是啥|是什么来着)[？?。]?", text)
    if seat:
        return "recall", text
    edit = re.fullmatch(r"(?:修改|更新|更正)记忆\s*[：:]\s*(.+?)\s*(?:→|->)\s*(.+)", text, re.S)
    if edit:
        return "edit", (edit[1], edit[2])
    correction = re.fullmatch(r"(?:以后|今后)(?:(?:我)?(?:坐)?(高铁|火车|飞机)(?:的)?(?:座位)?)?改成(靠窗|靠过道|靠走道|靠通道)[吧。！]?", text)
    if correction:
        return "seat_edit", (seat_topic(correction[1]) if correction[1] else None, correction[2])
    withdraw = re.fullmatch(r"(?:撤回记忆|删除记忆|忘记这条记忆|忘记)\s*[：:]\s*(.+)", text)
    if withdraw:
        return "forget", withdraw[1]
    forget = re.fullmatch(r"(?:请(?:你)?|帮我)?忘记\s*[：:]?\s*([a-f0-9]{32})", text)
    if forget:
        return "forget", forget[1]
    if re.match(r"^(?:请(?:你)?|帮我)?忘记\s*[：:]", text):
        raise ValueError("请在“忘记：”后输入完整的记忆 ID，可使用“列出记忆”查看")
    check = MEMORY_CHECK.fullmatch(text)
    if not check and (end := re.search(r"(?:了没(?:有)?|[吗嘛么])[？?](?![”’」』》'\"])", text)):
        check = MEMORY_CHECK.fullmatch(text[:end.end()])
    if not check:
        check = re.fullmatch(r"(?:请问[，,\s]*)?(?:你|您)?(?:有没有|是不是已经)记住(.*?)\s*[？?。]?", text)
    if check:
        return "check", check[1].strip()
    save = re.fullmatch(
        r"(?:请(?:你)?|帮我)?记住(?:\s*[：:]\s*|\s*[,，]?\s*"
        r"(?=我的|我叫|我是|我喜欢|我不喜欢|我希望|我习惯|我住在|我听歌|我平时|我通常|以后))(.+)",
        text, re.S,
    )
    if save:
        return "remember", save[1].strip()
    if re.fullmatch(r"(?:请(?:你)?|帮我)?记住\s*[：:]\s*", text):
        raise ValueError("请在“记住：”后输入要保存的内容")
    if target := saved_recall_request(text):
        return "recall_saved", target
    if (not ignore_personal_memory(text)
            and re.search(r"(?:按照?|根据|参照|结合)我(?:平时|之前|记录)?的?(?:高铁|火车|飞机)?(?:座位|选座)?(?:偏好|习惯)", text)
            and re.search(r"(?:选座|座位)(?:建议|推荐)|推荐.{0,5}座位|怎么选座|座位怎么选|选座位时该坐哪", text)
            and not re.search(r"字母|几[号排]|座号|车次|机型|票价|另外|顺便|比较|为什么|翻译|改写", text)):
        return "seat_advice", seat_topic(text)
    return None


def seat_advice(items, topic):
    if topic in (None, "seat"):
        matches = [item for item in items if item["topic"] in SEAT_NAMES]
    else:
        matches = [item for item in items if item["topic"] == topic] or [item for item in items if item["topic"] == "seat"]
    if not matches:
        return [], "目前没有保存适用于这次选座的偏好。"
    preferences = {}
    for item in matches:
        side = SEAT.search(item["text"])
        if side:
            position = "靠窗" if "窗" in side[0] else "靠过道"
            preferences.setdefault(item["topic"], set()).add(position)
    statements = [f"{SEAT_NAMES[scope]}优先选{next(iter(sides))}的位置。" if len(sides) == 1
                  else f"{SEAT_NAMES[scope]}有多条不同的偏好记录。"
                  for scope, sides in preferences.items()]
    return [item["id"] for item in matches], "".join(statements)
