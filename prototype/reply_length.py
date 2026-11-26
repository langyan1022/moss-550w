from dataclasses import dataclass
import re
import unicodedata

from cn2an import cn2an


NUMBER = r"(?:[0-9]{1,5}|[零〇一二两三四五六七八九十百千万]{1,10})"
QUOTED = re.compile(r'```[\s\S]*?```|`[^`\n]*`|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|《[^》]*》|"[^"\n]*"')
DIRECTIVE = r"用|写成|写出|写|给我(?:写)?|生成|改(?:写)?成|压缩(?:到|为)|缩(?:短)?到|控制在|限制在"
WRITE = re.compile(r"(?:^|[，,。；;]\s*)(?:请你?|帮我|麻烦你?|请帮我)?\s*"
                   r"(?:写|用|给我|改写|总结|概括|解释|说明|介绍|翻译|生成|压缩|缩短|回复)")
WRITE_AFTER = re.compile(r"^\s*(?:的)?\s*(?:来|去)?(?:写|改写|总结|概括|解释|说明|介绍|翻译|生成|压缩|缩短|回复|讲|说|回答)")
PER_PART = re.compile(r"每(?:一)?(?:段|节|条|项|步|章|句|部分|个(?:例子|段落))[^，,。；;\n]{0,12}$")
PARTS = re.compile(rf"(?P<count>{NUMBER})\s*(?:(?:个)?(?:步骤|要点|部分)|步|条|段|项)")
EXAMPLE = re.compile(rf"(?:再|并|另外|另外还)(?:给(?:出)?|写(?:出)?|举(?:出)?)\s*"
                     rf"(?:(?P<count>{NUMBER})\s*(?:个|条)\s*)?[^，,。；;\n]{{0,8}}例(?:子)?")
SCOPE = re.compile(
    rf"(?:(?P<directive>{DIRECTIVE})\s*(?:(?:一|个)(?:段|篇|份|则|封|条)\s*)?)?"
    rf"(?:(?P<label>正文|全文|字数|篇幅|总长|总共|合计)\s*[:：]?\s*)?"
    rf"(?:(?P<qualifier>不(?:得|能)?超过|至多|最多|上限(?:为)?|少于|不少于|至少|下限(?:为)?|大约|大概|大致|差不多|约|恰好|正好)\s*)?"
    rf"(?<![0-9零〇一二两三四五六七八九十百千万])(?P<first>{NUMBER})"
    rf"(?:\s*(?:[-–—~～]|到|至)\s*(?P<second>{NUMBER}))?"
    r"\s*字(?:符)?(?P<suffix>以内|以下|以上|左右)?"
)


def _part_budget(text, target):
    prompt = _instruction(text)
    for parts in PARTS.finditer(prompt):
        count = _part_count(parts["count"])
        if count <= 1:
            continue
        if example := EXAMPLE.search(prompt[parts.end():]):
            count += _part_count(example["count"] or "1")
        if 1 < count <= 10:
            per = target // count
            lo_part, hi_part = max(8, per - 4), per + 4
            return (f"正文分 {count} 项（每个必写步骤和例子各单独占 1 项），"
                    f"每项严格只写 1 句完整短句，控制在 {lo_part} 至 {hi_part} 字，写满 {count} 项立刻结束。")
    if 40 <= target <= 300:
        sentences = max(2, round(target / 21))
        per = (target + sentences - 1) // sentences
        return f"全文严格写满 {sentences} 句完整短句，每句控制在 {per} 至 {per + 4} 字，写完第 {sentences} 句立刻结束。"
    return ""


@dataclass(frozen=True)
class ReplyLength:
    lower: int | None
    upper: int | None
    approximate: bool = False

    def guidance(self, text):
        budget = ""
        if self.approximate:
            target = self.lower
            allowance = max(1, round(target * 0.08))
            lo, hi = max(1, target - allowance), target + allowance
            constraint = f"约 {target} 字，正文必须落在 {lo} 至 {hi} 字之间（不要少于 {lo} 字，也不要超过 {hi} 字）"
            budget = _part_budget(text, target)
        elif self.lower == self.upper:
            constraint = f"恰好 {self.lower} 字"
        elif self.lower is not None and self.upper is not None:
            target = (self.lower + self.upper) // 2
            constraint = f"{self.lower} 至 {self.upper} 字，目标 {target} 字（不要少于 {self.lower} 字，也不要超过 {self.upper} 字）"
            budget = _part_budget(text, target)
        elif self.upper is not None:
            target = max(1, self.upper - max(1, self.upper // 8))
            constraint = f"最多 {self.upper} 字，目标 {target} 字，严禁超过 {self.upper} 字"
        else:
            constraint = f"至少 {self.lower} 字，完整写到即可结束"
        return (
            f"逐项满足用户要求，整篇正文的长度是 {constraint}。标点、列表标签和例子都计入正文。"
            + budget +
            "只输出完成后的中文正文，严禁输出任何打草稿、字数核算、英文分析或逐句核对过程；每项用简短具体的话写清楚。"
            "直接从第一个步骤或要点开始，最后一个必写要点完成后结束。"
            "省去开场复述、额外建议、未要求的时限和额外总结，不要使用 Markdown 标记符号（如 #、*、`）。"
        )


def _quantity(raw):
    return int(raw) if raw.isascii() else cn2an(raw, "normal")


def _part_count(raw):
    try:
        return _quantity(raw)
    except ValueError:
        return 0


def _instruction(text):
    plain = unicodedata.normalize("NFKC", text)
    plain = QUOTED.sub(lambda match:" " * len(match[0]), plain)
    return re.split(r"(?:原文|以下正文|正文如下)\s*[:：]", plain, maxsplit=1)[0].strip()


def reply_length(text):
    plain = _instruction(text)
    lower = upper = target = None
    for match in SCOPE.finditer(plain):
        qualifier = match["qualifier"] or ""
        suffix = match["suffix"] or ""
        if PER_PART.search(plain[:match.start()]) or re.match(r"\s*(?:了(?:吗|么|没有)|吗|么|呢|是什么|是多少)", plain[match.end():]):
            continue
        has_write = bool(WRITE.search(plain) or WRITE_AFTER.match(plain[match.end():]))
        if not (match["directive"] or (match["label"] and (match.start() == 0 or has_write)) or
                ((qualifier or suffix or match["second"]) and (has_write or match[0].strip() == plain.rstrip("。！!")))):
            continue
        try:
            first = _quantity(match["first"])
            second = _quantity(match["second"]) if match["second"] else None
        except ValueError:
            continue
        if first < 1 or (second is not None and (second < first or qualifier or suffix)):
            continue
        if second is not None:
            lower, upper = max(lower or 0, first), min(upper or second, second)
        elif qualifier.startswith(("不超过","不得超过","不能超过","至多","最多","上限","少于")) or suffix in {"以内","以下"}:
            cap = first - int(qualifier == "少于")
            upper = cap if upper is None else min(upper, cap)
        elif qualifier.startswith(("不少于","至少","下限")) or suffix == "以上":
            lower = max(lower or 0, first)
        elif qualifier in {"恰好","正好"}:
            lower, upper = max(lower or first, first), min(upper or first, first)
        else:
            target = first
    if (upper is not None and upper < 1) or (lower is not None and upper is not None and lower > upper):
        return None
    if lower is not None or upper is not None:
        return ReplyLength(lower, upper)
    return ReplyLength(target, target, approximate=True) if target is not None else None
