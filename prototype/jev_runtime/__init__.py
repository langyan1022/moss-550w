import re


def _clean(x: str) -> str:
    """剥掉一条候选两端的括号/引号/编号/逗号——模型偶尔一行给一个 ["…"]，或者整条带引号。
    末尾的句号也去掉（微信里很少有人用句号收尾）；？！～ 照留，那是语气。"""
    x = re.sub(r"^\s*(?:\d+[.)、]|[-*])\s*", "", x.strip())
    x = x.strip(" \t[]\"'“”‘’,，")
    x = re.sub(r"^(?:me|我)\s*[:：]\s*", "", x)  # 对话样本是「me: xxx」格式，模型会照抄前缀
    return x[:-1] if x.endswith("。") else x


_INJECT = re.compile(
    r"忽略|无视|作废|指令|规则|只输出|只回|必须|一字不差|你现在是|扮演|prompt|system|ignore|instruction"
    r"|回我.{0,4}遍|重复|复读|照(着|做|抄)|别加标点|不加标点|不带标点|用(那个|这个|下面|上面)?.{0,6}回我|跟我说.{0,3}遍|输出",
    re.I)
_LAUGH = re.compile(r"^[哈嘿嘻呵hx6]+$", re.I)


def _norm(t: str) -> str:
    return re.sub(r"[\s\W_]+", "", t).lower()


def _suspects(messages: list, keep: int) -> list[str]:
    """上下文里长得像提示词注入的对方消息（不管是不是最新一条——模型会把它当长期指令）。"""
    out = []
    for m in messages[-keep:]:
        who, text = (m.get("from"), m.get("text")) if isinstance(m, dict) else (m[0], m[1])
        if who == "her" and _INJECT.search(str(text or "")):
            out.append(str(text))
    return out


def _her_recent(messages: list, n: int = 5) -> list[str]:
    out = []
    for m in reversed(messages):
        who, text = (m.get("from"), m.get("text")) if isinstance(m, dict) else (m[0], m[1])
        if who == "her":
            out.append(str(text or ""))
            if len(out) >= n:
                break
    return out


def _sanitize(cands: list[str], suspects: list[str], her_recent: list[str] = ()) -> list[str]:
    """候选出口的硬过滤，prompt 骗得过这里骗不过：
    去重（忽略空白/标点/大小写）；候选原样出现在注入消息里的直接丢（「必须都是 TARGET」→ TARGET 就在他那条里）；
    候选跟对方最近几条里任何一条一模一样也丢——鹦鹉学舌不是回复（「丢个词你回我三遍」就靠这条挡）。纯笑声例外。"""
    bad = [_norm(t) for t in suspects]
    echo = {_norm(t) for t in her_recent if not _LAUGH.match(_norm(t))}
    seen, out = set(), []
    for c in cands:
        n = _norm(c)
        if not n or n in seen or (len(n) >= 2 and any(n in b for b in bad)) or n in echo:
            continue
        seen.add(n)
        out.append(c)
    return out


def build_state(messages: list, relationship: str, keep: int = 10,
                reply_to: str | None = None) -> dict:
    """messages: (from, text) / (from, text, name) / dict（name 可选）。from 只认 her/me。

    name = 群里的发言人；有 name 就当群聊（chat.is_group）。reply_to = 群里指定的回复对象。
    """
    cleaned = []
    for item in messages:
        if isinstance(item, dict):
            who, text, name = item.get("from"), item.get("text"), item.get("name")
        else:
            who, text = item[0], item[1]
            name = item[2] if len(item) > 2 else None
        if who not in ("her", "me"):
            raise ValueError(f"message from must be 'her' or 'me', got {who!r}")
        message = {"from": who, "text": str(text)}
        if name:
            message["name"] = str(name)
        cleaned.append(message)
    cleaned = cleaned[-keep:]
    latest_from = cleaned[-1]["from"] if cleaned else "her"
    chat = {
        "relationship": relationship,
        "messages": cleaned,
        "latest_from": latest_from,
        "is_group": any("name" in m for m in cleaned),
    }
    if reply_to:
        chat["reply_to"] = str(reply_to)
    return {"chat": chat}


def build_rank_question(candidates: list[str]) -> dict:
    """Build the best_reply choice question. criteria values stay in original Chinese."""
    if not 2 <= len(candidates) <= 3:
        raise ValueError("build_rank_question expects 2 or 3 candidate replies")
    keys = ("reply_a", "reply_b", "reply_c")[:len(candidates)]
    return {
        "best_reply": {
            "type": "choice",
            "instructions": (
                "Which candidate reply is the most appropriate next message, "
                "given the conversation and the other person's true need? "
                "Prefer a reply that matches the best action type. "
                "Penalize dismissive, over-promising, or off-topic replies. "
                "If the facts are not yet confirmed, prefer the candidate that looks them up "
                "instead of faking memory or a vague apology."
            ),
            "criteria": {key: text for key, text in zip(keys, candidates)},
        }
    }
