from datetime import date, time
import re

from cn2an import an2cn


DECIMAL = r"[0-9]{1,12}(?:\.[0-9]{1,12})?"
GROUPED_DECIMAL = r"(?:[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,6})?|[0-9]{1,12}(?:\.[0-9]{1,12})?)"
NUMBER = rf"[+\-−]?{DECIMAL}"
UNIT = r"(?:°[ \t]*[Cc]|℃|[%％])"
TECH_UNIT = r"(?:GiB|GB|MiB|MB|KiB|KB|TiB|TB|ms|km/h)"
SPACE = r"[ \t]*"
START = r"(?<![-a-zA-Z0-9_./:\\])"
DATE = (r"(?P<paren>[(（])?(?P<year>[0-9]{4})-(?P<month>[0-9]{1,2})-(?P<day>[0-9]{1,2})"
        r"(?![a-zA-Z0-9_]|[./-][0-9])(?(paren)[）)])")
MEASURE = (rf"(?:(?P<first>{NUMBER}){SPACE}(?P<first_unit>{UNIT})?{SPACE}"
           rf"(?:到|至|～|~|–|—|-){SPACE})?(?P<last>{NUMBER}){SPACE}(?P<unit>{UNIT})(?![A-Za-z0-9_])")
TECH_MEASURE = (rf"(?P<tech_val>{GROUPED_DECIMAL}){SPACE}(?P<tech_unit>{TECH_UNIT})(?![A-Za-z0-9_/])")
CURRENCY = rf"(?P<curr>[¥￥$]){SPACE}(?P<curr_val>{GROUPED_DECIMAL})(?![A-Za-z0-9_])"
ITEM_ID = r"(?P<item_noun>便签|提醒|日程|任务|条目|规则|经验|节点)[ \t]*#(?P<item_id>[0-9]{1,6})(?![0-9A-Za-z_])"
TOP_RANK = r"(?:(?<=[\u3400-\u9fff])[ \t]+)?(?<![A-Za-z0-9_])(?:Top|TOP|top)[ \t]*(?P<top_n>[1-9]|1[0-9]|20)(?![0-9A-Za-z_])"
CN_YMD = r"(?<![0-9A-Za-z_./:\\-])(?P<cny>19[0-9]{2}|2[01][0-9]{2})年(?P<cnm>1[0-2]|0?[1-9])月(?P<cnd>3[01]|[12][0-9]|0?[1-9])(?P<cn_day_unit>[日号])(?!，星期[一二三四五六日]，[0-9]{2}:[0-9]{2})"
CN_MD = r"(?<![0-9A-Za-z_./:\\-年])(?P<md_m>1[0-2]|0?[1-9])月(?P<md_d>3[01]|[12][0-9]|0?[1-9])(?P<md_unit>[日号])(?![0-9])"
CN_YEAR = r"(?<![0-9A-Za-z_./:\\-])(?P<bare_y>19[0-9]{2}|2[01][0-9]{2})[ \t]*年(?![ \t]*[0-9]{1,2}[ \t]*月)"
CN_COUNTER = (
    r"(?<![0-9A-Za-z_./:\\−-])(?P<cnt_n>[0-9]{1,7})[ \t]*"
    r"(?P<cnt_unit>分钟|小时|秒钟|个月|周年|公里|公斤|毫升|克|米|天|周|岁|次|遍|条|个|项|名|位|席|家|首|句|段|轮|步|件|张|份|本|台|辆|块|倍|人|元|"
    r"万|亿|余|多|种|瓦|伏|安|吨|升|页|章|节|行|字|词|期|届|层|楼|排|架|艘|座|颗|枚|部|套|例|起|批|度)"
    r"(?![0-9A-Za-z_/])"
)
LIANG_COUNTERS = frozenset({
    "小时", "个月", "周年", "公里", "公斤", "天", "周", "次", "遍", "条", "个", "项",
    "名", "位", "席", "家", "首", "句", "段", "轮", "步", "件", "张", "份", "本",
    "台", "辆", "块", "倍", "人", "万", "亿", "种", "吨", "升", "层", "架", "艘",
    "座", "颗", "枚", "部", "套", "例", "起", "批",
})
GROUPED_NUM = r"(?P<grouped>[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,6})?)(?![0-9A-Za-z_])"
ENGINE_CODE = (
    r"(?<![0-9A-Za-z_./:\\-])(?P<code_n>[0-9]{3,8})[ \t]*"
    r"(?P<code_unit>号(?:发动机|行星发动机|机|仓|门|线|井|闸)?|发动机|行星发动机|机)"
    r"(?![0-9A-Za-z_])"
)
STANDALONE_DIGITS = (
    r"(?<![0-9A-Za-z_./:\\-])(?P<digits_n>[0-9]{4,11})"
    r"(?![0-9A-Za-z_./:\\-])"
)
HAN = r"[\u3400-\u9fff]"
# The local clock and the current-weather formatter provide these exact shapes.
CLOCK_STAMP = (r"(?:(?<=[。\n])|^)(?P<clock_date>[0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日)，"
               r"(?P<weekday>星期[一二三四五六日])，"
               r"(?P<clock_time>[0-9]{2}:[0-9]{2}:[0-9]{2}|[0-9]{1,2}点[0-9]{1,2}分[0-9]{1,2}秒)(?=。|$)")
WEATHER_STAMP = (r"(?<=当地)[ \t]+(?P<weather_date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
                 r"[ \t]+(?P<weather_time>[0-9]{2}:[0-9]{2})(?=，)")
WEATHER_MEASURE = (rf"(?:(?<=[，。\n])|^)(?:降水[ \t]+(?P<rain>{DECIMAL})[ \t]+毫米"
                   rf"|风速[ \t]+(?P<wind>{DECIMAL})[ \t]+公里/小时)(?=[，。\n]|$)")
DAYPART = r"(?:凌晨|清晨|早晨|上午|中午|正午|午间|下午|傍晚|晚上|晚间|午夜)"
TIME_LABEL = rf"(?:{DAYPART}|日出(?:时间)?|日落(?:时间)?)"
TIME_VALUE = r"[0-9]{1,2}[:：][0-9]{2}(?:[:：][0-9]{2})?"
TIME_END = r"(?![a-zA-Z0-9_+:/：\\]|[.-][a-zA-Z0-9_])"
TIME_RANGE_SEPARATOR = r"(?:到|至|[-–—~～])"
TWELVE_HOUR_DAYPARTS = {"下午":range(13, 19), "傍晚":range(17, 20), "晚上":range(18, 24), "晚间":range(18, 24)}
CONTEXT_CLOCK = (
    rf"{START}(?P<time_label>{TIME_LABEL}){SPACE}(?:[：:]{SPACE})?"
    rf"(?P<time_at>{TIME_VALUE})"
    rf"(?:{SPACE}(?P<time_separator>{TIME_RANGE_SEPARATOR}){SPACE}"
    rf"(?:(?P<time_end_label>{TIME_LABEL}){SPACE})?(?P<time_end>{TIME_VALUE}))?"
    rf"{TIME_END}(?!{SPACE}{TIME_RANGE_SEPARATOR}{SPACE}(?:{TIME_LABEL}{SPACE})?[0-9]{{1,2}}[:：])"
    rf"(?:[ \t]+(?={HAN}))?"
)
TOKENS = re.compile(rf"(?P<literal>https?://[^\s，。！？；（）]+|`[^`\n]*`)|"
                    rf"{CLOCK_STAMP}|{WEATHER_STAMP}|{WEATHER_MEASURE}|{CONTEXT_CLOCK}|"
                    rf"{ITEM_ID}|{TOP_RANK}|{CN_YMD}|{CN_MD}|(?:(?<={HAN})[ \t]+)?(?:{CN_YEAR}|{CN_COUNTER}|{ENGINE_CODE})|"
                    rf"{START}(?:(?<={HAN})[ \t]+)?(?:{CURRENCY}|{DATE}|{MEASURE}|{TECH_MEASURE}|{GROUPED_NUM})(?:[ \t]+(?={HAN}))?|"
                    rf"{STANDALONE_DIGITS}")


def spoken_decimal(number):
    number = number.replace(",", "")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return an2cn(number)


def spoken_tech_measure(value, unit):
    spoken = spoken_decimal(value)
    if unit in ("GiB", "GB"):
        return spoken + " G"
    if unit in ("TiB", "TB"):
        return spoken + " T"
    if unit in ("MiB", "MB"):
        return spoken + "兆"
    if unit in ("KiB", "KB"):
        return spoken + " K"
    if unit == "ms":
        return spoken + "毫秒"
    if unit == "km/h":
        return "每小时" + spoken + "公里"
    return spoken + unit


def spoken_measure(value, unit):
    negative = value.startswith(("-", "−"))
    spoken = spoken_decimal(value.lstrip("+-−"))
    if unit in ("%", "％"):
        return ("负" if negative else "") + "百分之" + spoken
    return ("零下" if negative else "正" if value.startswith("+") else "") + spoken + "摄氏度"


def spoken_date(year, month, day):
    value = date(int(year), int(month), int(day))
    return f"{an2cn(year, 'direct')}年{an2cn(value.month)}月{an2cn(value.day)}日"


def clock_parts(text):
    parts = tuple(int(part) for part in re.findall(r"[0-9]+", text))
    time(*parts)
    return parts


def spoken_clock_parts(parts):
    return "".join(("零" if unit != "点" and 0 < part < 10 else "") + an2cn(part) + unit
                   for part, unit in zip(parts, "点分秒"))


def spoken_time(text):
    return spoken_clock_parts(clock_parts(text))


def spoken_context_time(parts, short_hour):
    hour, minute, *seconds = parts
    hour = hour - 12 if short_hour else hour
    clock = spoken_clock_parts((hour, minute, *seconds))
    if clock.startswith("二点"):
        clock = "两" + clock[1:]
    if not seconds and minute in (0, 30):
        return clock.split("点", 1)[0] + ("点整" if minute == 0 else "点半")
    return clock


def spoken_time_context(match):
    label, end_label, end = match["time_label"], match["time_end_label"], match["time_end"]
    first = clock_parts(match["time_at"])
    last = clock_parts(end) if end else None
    hours = TWELVE_HOUR_DAYPARTS.get(label, ())
    short_first = first[0] in hours
    short_last = last and last[0] in TWELVE_HOUR_DAYPARTS.get(end_label, ())
    if last and not end_label:
        # The first daypart applies to both hours only if the whole range fits it.
        short_first = short_last = short_first and last[0] in hours and first <= last
    reading = label + spoken_context_time(first, short_first)
    if last:
        separator = match["time_separator"]
        reading += separator if separator in ("到", "至") else "到"
        reading += (end_label or "") + spoken_context_time(last, short_last)
    return reading


def spoken_numbers(text):
    def expand(match):
        if match["literal"]:
            return match[0]
        if stamp := match["clock_date"] or match["weather_date"]:
            try:
                day = spoken_date(*re.findall(r"[0-9]+", stamp))
                clock = spoken_time(match["clock_time"] or match["weather_time"])
            except ValueError:
                return match[0]
            if match["weekday"]:
                return f"{day}，{match['weekday']}，{clock}"
            return f"时间{day}{clock}"
        if match["rain"]:
            return "降水" + spoken_decimal(match["rain"]) + "毫米"
        if match["wind"]:
            return "风速每小时" + spoken_decimal(match["wind"]) + "公里"
        if match["time_at"]:
            try:
                return spoken_time_context(match)
            except ValueError:
                return match[0]
        if match["item_id"]:
            return f"{match['item_noun']}{an2cn(match['item_id'])}号"
        if match["top_n"]:
            return "前" + an2cn(match["top_n"])
        if match["cny"]:
            try:
                value = date(int(match["cny"]), int(match["cnm"]), int(match["cnd"]))
            except ValueError:
                return match[0]
            return f"{an2cn(match['cny'], 'direct')}年{an2cn(value.month)}月{an2cn(value.day)}{match['cn_day_unit']}"
        if match["md_m"]:
            try:
                value = date(2024, int(match["md_m"]), int(match["md_d"]))
            except ValueError:
                return match[0]
            return f"{an2cn(value.month)}月{an2cn(value.day)}{match['md_unit']}"
        if match["bare_y"]:
            return f"{an2cn(match['bare_y'], 'direct')}年"
        if match["cnt_n"]:
            num = match["cnt_n"]
            unit = match["cnt_unit"]
            if num.startswith("0") and len(num) > 1:
                return an2cn(num, "direct") + unit
            if num == "2" and unit in LIANG_COUNTERS:
                return "两" + unit
            return an2cn(num) + unit
        if match["curr"]:
            return spoken_decimal(match["curr_val"]) + ("美元" if match["curr"] == "$" else "元")
        if match["tech_unit"]:
            return spoken_tech_measure(match["tech_val"], match["tech_unit"])
        if match["grouped"]:
            return spoken_decimal(match["grouped"])
        if match["code_n"]:
            return an2cn(match["code_n"], "direct") + match["code_unit"]
        if match["digits_n"]:
            return an2cn(match["digits_n"], "direct")
        if match["year"]:
            try:
                value = spoken_date(match["year"], match["month"], match["day"])
            except ValueError:
                return match[0]
            if match["paren"]:
                before, after = text[:match.start()].rstrip(), text[match.end():].lstrip()
                if before and before[-1] not in "，,。！？；：":
                    value = "，" + value
                if after and after[0] not in "，,。！？；：":
                    value += "，"
            return value
        last = spoken_measure(match["last"], match["unit"])
        if not match["first"]:
            return last
        first = spoken_measure(match["first"], match["first_unit"] or match["unit"])
        return first + "到" + last

    return TOKENS.sub(expand, text)
