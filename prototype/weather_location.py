import re

from colloquial_zh import COLLOQUIAL_DAY_PATTERNS, normalize_colloquial_days, normalize_weather_erhua
from weather_places import district_matches, known_city


NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
LABEL = re.compile(r"(?<!经)纬度|(?<!纬)经度|北纬|南纬|东经|西经|(?<![A-Za-z])(?:latitude|longitude|lat|lon)(?![A-Za-z])", re.I)
FIGURES = r"[\d零〇一二两三四五六七八九十百千万亿]+"
ANGLE_NUMBER = rf"[+正负−-]?(?:{FIGURES}(?:[.点]{FIGURES})?|[.点]{FIGURES})"
ANGLE = re.compile(rf"\s*[:：=是为]?\s*({ANGLE_NUMBER})(?:\s*[°度]"
                   rf"(?:\s*({ANGLE_NUMBER})\s*(?:分|′(?!′)|'(?!')))?"
                   rf"""(?:\s*({ANGLE_NUMBER})\s*(?:秒|″|"|′′|''))?)?""")
ANGLE_TAIL = re.compile(rf"""\s*(?:[°度分秒′″'"点半之\d]|\.\s*[.\d]|(?:到|至|[~～—])\s*{ANGLE_NUMBER}"""
                        rf"""|{ANGLE_NUMBER}\s*(?=[度分秒′″'"，,、。？?]|的|今天|明天|后天|天气|气温|{LABEL.pattern}|$))""", re.I)
GPS = re.compile(rf"({NUMBER})\s*°?\s*([NS])[,，\s]+({NUMBER})\s*°?\s*([EW])(?![A-Za-z])", re.I)
PAIR = re.compile(rf"(?<![\d.])({NUMBER})\s*[,，]\s*({NUMBER})(?![\d.])")
DAY_LABELS = {"今天": "today", "明天": "tomorrow", "后天": "day_after_tomorrow",
              "当前": "current", "现在": "current", "三天": "three_days"}
PLACE = re.compile(r"[\u3400-\u9fffA-Za-z][\u3400-\u9fffA-Za-z .·-]{0,46}")
PREFIX = re.compile(r"^(?:(?:请问|请你?|帮我|麻烦你?|给我|告诉我|我想知道|我想|我在|查一查|查一下|查询|查看|查查|查|看看)\s*)+")
DAY = re.compile(r"(?:未来三天|这三天|大后天|大前天|今天|明天|后天|昨天|前天|现在|当前)")
OUTSIDE_FORECAST_DAY = re.compile(r"大后天|大前天|昨天|前天|(?:下周|下星期)[一二三四五六七日天1-7]?|下个月")
FORECAST_RANGE_REPLY = "天气服务支持今天、明天和后天的预报。"
SIMPLE = re.compile(r"(.{0,70}?)(?:的)?(?:天气(?:预报)?|气温|温度)(?:怎么样|如何|多少度|是多少|呢)?")
FACT_END = re.compile(r"(?:天气(?:预报)?(?:怎么样|如何|好不好)?|(?:气温|温度)?(?:是多少度|多少度|几度)|"
                      r"(?:会不会|会|要|有)?(?:下雨|下雪|降水)(?:了|吗|么)?)[呀呢啊吧？?。！!\s]*$")
NO_LOOKUP = re.compile(r"不要查|不用查|别查|无需查|翻译|复述|举例|什么是天气|解释天气")
OTHER_REQUEST = re.compile(r"比较|对比|区别|英文|英语|[和与].{1,22}(?:哪个|哪里|分别)|分析|建议|适合|怎么穿|穿什么|带伞")
BROWSER_REFERENCE = re.compile(r"(?:我(?:的)?)?(?:当前(?:的)?(?:定位点|定位|位置)|现在的?位置|本机位置|所在地)(?:附近)?|我这(?:儿|里|边)|这里|这儿|附近")
DEVICE_LOCATION = re.compile(
    r"(?:(?:MOSS|莫斯)[,、\s]+)?(?:(?:请问|请|帮我|麻烦你?|给我|(?:你能)?告诉我|查一下|查查|查看|看看|你知道)\s*)*"
    r"(?:(?:(?:我|本机|设备|浏览器)(?:的?(?:当前|现在|此刻))?|(?:当前|现在|此刻))的?"
    r"(?:地理位置|定位点|定位|位置|所在地|经纬度|坐标)"
    r"(?:是?(?:在)?(?:哪里|哪儿|哪)|是什么|是多少|什么|多少|的坐标|的经纬度)?|"
    r"我(?:现在|当前|此刻)?(?:在|位于)(?:哪里|哪儿|哪))"
    r"(?:吗|么|呢)?(?:[,，]\s*(?:你(?:不)?知道(?:么|吗)?|能告诉我吗))?[。？?！!\s]*", re.I
)
PARTS = {"凌晨": (0, 6), "早上": (6, 10), "上午": (6, 12), "中午": (11, 14), "下午": (12, 18),
         "傍晚": (17, 20), "晚间": (18, 24), "晚上": (18, 24), "夜里": (18, 24)}
PART_OF_DAY = re.compile("|".join(PARTS))
FORECAST_FACT = re.compile(
    r"(?:天气(?:预报|情况)?(?:怎么样|如何|怎样|好不好|冷不冷|热不热|暖不暖和|凉不凉|凉快不凉快)?|"
    r"(?:气温|温度)(?:怎么样|如何|多少度|是多少)?|"
    r"(?:气温|温度)?(?:几度|多少度)|(?:冷不冷|热不热|暖不暖和|凉不凉|凉快不凉快|刮不刮风)|"
    r"(?:会不会|会|要不要|要|有没有|有)?(?:下雨|下雪|降雨|降水)"
    r"(?:概率(?:是多少|多高|多大|多少)?|大不大)?|(?:出门)?(?:需不需要|需要|要不要|要|用不用|用|建议)?带伞|"
    r"(?:出门)?(?:怎么穿|穿什么|穿衣建议))"
    r"(?:吗|么|呢|啊|呀|吧)?$")
LOCATION_REFERENCE = re.compile(
    r"(?:(?:再说说|回到|换回|说回)\s*)?"
    r"(?:(?:刚才|刚刚|之前|前面|上面|上次|上回)(?:查过|查的|提到的|说的)?(?:的)?"
    r"(?:(?:那个|那组|那座|这个|这组|这座)?(?:地点|地方|位置|坐标|经纬度|城市|区县|区|县|市)|那里|那边|那儿)|"
    r"那个(?:区县|地方|地点|城市|区|县)|那组(?:坐标|经纬度)|那里|那边|那儿)")
FORECAST_FOLLOWUP = re.compile(
    r"(?:那(?:么)?|再看看|看看)?(?:大后天|后天|明天|今天|当前|现在|未来三天|这三天)?的?"
    r"(?:天气(?:预报)?|气温|温度|降水概率|(?:会不会|会|要)?(?:下雨|下雪)|"
    r"(?:气温|温度)?(?:多少度|几度)|(?:出门)?(?:需不需要|需要|要不要|要|用不用|用)?带伞|"
    r"(?:出门)?(?:穿什么|怎么穿))"
    r"(?:怎么样|如何|是多少|多少|多高)?[呢吗么呀啊吧]*")


def forecast_reference(text):
    text = normalize_weather_erhua(text)
    match = LOCATION_REFERENCE.search(text)
    remainder = text[:match.start()] + text[match.end():] if match else text
    remainder = re.sub(r"[，,。？?！!\s]", "", remainder).lstrip("的")
    day_only = re.fullmatch(r"(?:大后天|后天|明天|今天|现在)(?:的呢|呢)?", remainder)
    resume = match and not remainder and re.match(r"再说说|回到|换回|说回", match[0])
    return bool(match), bool(FORECAST_FOLLOWUP.fullmatch(remainder) or (match and day_only) or resume)


def forecast_details(text):
    """Recognize concise forecasts and advice without treating unrelated clauses as weather."""
    cleaned = LOCATION_REFERENCE.sub("", PREFIX.sub("", normalize_weather_erhua(text).strip()), count=1)
    if OUTSIDE_FORECAST_DAY.search(cleaned):
        return "", None, False
    part = PART_OF_DAY.search(cleaned)
    explicit = ""
    has_request = False
    for clause in re.split(r"[，,。；;？！?!\n]", cleaned):
        clause = DAY.sub("", PART_OF_DAY.sub("", BROWSER_REFERENCE.sub("", clause))).strip().lstrip("的").strip()
        if not clause:
            continue
        clause = re.sub(r"^(?:那(?:么)?|再看(?:看)?|在|去|出门的时候|这)", "", clause).strip()
        fact = FORECAST_FACT.search(clause)
        if not fact:
            return "", part[0] if part else None, False
        place = clause[:fact.start()].strip().removesuffix("的").strip()
        if place:
            if not (known_city(place) or district_matches(place)) or (explicit and place != explicit):
                return "", part[0] if part else None, False
            explicit = place
        has_request = True
    return explicit, part[0] if part else None, has_request


def referenced_kind(text):
    match = LOCATION_REFERENCE.search(text)
    if match:
        if match[0].endswith(("坐标", "经纬度")):
            return "coordinates"
        if match[0].endswith(("区", "县")):
            return "district"
        if match[0].endswith("市"):
            return "city"
    return None


def point(latitude, longitude):
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("纬度需要在 −90 到 90 之间，经度需要在 −180 到 180 之间。")
    return {"name": f"纬度 {round(latitude, 10)}°、经度 {round(longitude, 10)}°",
            "latitude": latitude, "longitude": longitude, "kind": "coordinates"}


def browser_position(value):
    if not isinstance(value, dict) or value.keys() != {"latitude", "longitude"}:
        raise ValueError("浏览器位置需要且只接受纬度和经度。")
    latitude, longitude = value["latitude"], value["longitude"]
    if (type(latitude) not in {int, float} or type(longitude) not in {int, float}
            or not (-90 <= latitude <= 90 and -180 <= longitude <= 180)):
        raise ValueError("浏览器经纬度无效。")
    return {"name": "当前位置附近", "latitude": latitude, "longitude": longitude, "kind": "browser"}


def angle_number(token):
    if token.startswith(("-", "−", "负")):
        raise ValueError("度分秒各项需要使用正数或零，南北与东西由度数前的符号或方位表示。")
    token = token.lstrip("+正")
    if re.fullmatch(NUMBER, token):
        return float(token)
    from cn2an import cn2an
    try:
        return float(cn2an(token, "smart"))
    except ValueError as error:
        raise ValueError("坐标中的数字没有读清，请再说一次度数或度分秒。") from error


def angle_value(text, end, hemisphere):
    match = ANGLE.match(text, end)
    if not match or ANGLE_TAIL.match(text, match.end()):
        raise ValueError("请提供完整坐标，例如北纬 31 度 14 分、东经 121 度 28 分。")
    raw, minutes, seconds = match.groups()
    negative = raw.startswith(("-", "−", "负"))
    if hemisphere and negative:
        raise ValueError("带有南北纬或东西经标记时，请使用正数表示度数。")
    degrees = angle_number(raw.lstrip("+-正负−"))
    if minutes is not None or seconds is not None:
        minute = angle_number(minutes) if minutes else 0
        second = angle_number(seconds) if seconds else 0
        if not (0 <= minute < 60 and 0 <= second < 60):
            raise ValueError("坐标的分和秒需要大于等于 0、小于 60。")
        if not degrees.is_integer() or (seconds is not None and minutes is not None and not minute.is_integer()):
            raise ValueError("请使用十进制度，或只在度分秒的最后一项使用小数。")
        degrees += minute / 60 + second / 3600
    return -degrees if negative or hemisphere in {"南纬", "西经"} else degrees


def coordinates(text):
    labels = list(LABEL.finditer(text))
    if labels:
        values = {}
        for match in labels:
            label = match[0].lower()
            axis = "lat" if label in {"北纬", "南纬", "纬度", "latitude", "lat"} else "lon"
            if axis in values:
                raise ValueError("请各写一次纬度和经度，例如北纬 39.92、东经 116.44。")
            hemisphere = label if label in {"东经", "西经", "南纬", "北纬"} else None
            values[axis] = angle_value(text, match.end(), hemisphere)
        if len(values) == 2:
            return point(values["lat"], values["lon"])
        raise ValueError("查询坐标天气需要同时提供纬度和经度。")
    gps = GPS.search(text)
    if gps:
        lat, ns, lon, ew = gps.groups()
        if float(lat) < 0 or float(lon) < 0:
            raise ValueError("带有南北纬或东西经标记时，请使用正数表示度数。")
        return point(float(lat) * (-1 if ns.upper() == "S" else 1), float(lon) * (-1 if ew.upper() == "W" else 1))
    pair = PAIR.search(text)
    if pair:
        first, second = map(float, pair.groups())
        if not re.search(r"坐标|经纬度|纬经度", text):
            before = PREFIX.sub("", DAY.sub("", text[:pair.start()])).strip(" （(")
            after = DAY.sub("", text[pair.end():]).strip(" ）)的？?。!")
            if before or after not in {"", "天气", "天气怎么样", "天气预报", "气温"}:
                return None
        if "经纬度" in text or abs(first) > 90 >= abs(second):
            return point(second, first)
        return point(first, second)
    if re.search(r"纬度|经度|坐标|北纬|东经|西经|南纬", text):
        raise ValueError("请提供完整坐标，例如纬度 39.92、经度 116.44。")
    return None


def period_from(text, default="current"):
    text = BROWSER_REFERENCE.sub("", normalize_weather_erhua(text))
    if OUTSIDE_FORECAST_DAY.search(text):
        return default
    periods = [value for label, value in DAY_LABELS.items() if label in text]
    return "three_days" if len(set(periods)) > 1 else next(iter(periods), default)


def place_text(text):
    text = PREFIX.sub("", normalize_weather_erhua(text).strip(" ？?。！!"))
    simple = SIMPLE.fullmatch(text)
    if not simple:
        return ""
    place = DAY.sub("", simple[1]).strip().rstrip("的").strip()
    if (not PLACE.fullmatch(place)
            or re.search(r"还是|以及|用英|翻译|为什么|介绍|解释|哪里|哪儿|我这|当地|这里|这儿|那儿|附近|最近|刚才|别查|不要", place)):
        return ""
    return place


def weather_place_answer(text):
    """Read a place-only reply after a weather location question."""
    plain = text.strip(" \t\r\n。？?！!")
    label = re.match(r"^(?:我(?:现在)?在|(?:地点|城市|区县)(?:是|[:：]))\s*", plain)
    if label:
        plain = plain[label.end():]
    plain = re.sub(r"(?:就(?:这里|这个地方)|这(?:个)?地方|呢|吧)$", "", plain).strip()
    if known_city(plain) or district_matches(plain):
        return plain
    # Recognize an explicit place reply even when the local index has no coordinates for it.
    if ((label or plain.endswith(("市", "区", "县", "旗"))) and PLACE.fullmatch(plain)
            and not re.search(r"请|查|天气|怎么|为什么|哪里|播放|关闭|打开|屏幕|文件", plain)):
        return plain
    return ""


def weather_coordinate_answer(text):
    plain = text.strip(" \t\r\n。？?！!")
    value = re.sub(r"^(?:经纬度|坐标)\s*[:：=]?\s*", "", plain)
    if PAIR.fullmatch(value) or GPS.fullmatch(value):
        return coordinates(plain)
    # Each remaining component must be coordinate notation. Ignore names and commands.
    if LABEL.search(value) and re.fullmatch(rf"(?:{LABEL.pattern}|{ANGLE_NUMBER}|[\s:：=是为,，、°度分秒′″'\"])+", value, re.I):
        return coordinates(value)
    return None


def districts(query, previous=None):
    matches = district_matches(query)
    if len(matches) > 1 and previous:
        former = (previous.get("parent") or previous.get("city", "")).removesuffix("市")
        narrowed = [item for item in matches if former and item["name"].startswith(former)]
        if len(narrowed) == 1:
            return narrowed
    return matches


def city_candidates(query, results):
    query = query.strip().removesuffix("市").casefold()
    exact = [p for p in results if p["name"].removesuffix("市").casefold() == query]
    candidates = exact or results
    if len(candidates) <= 1:
        return candidates
    capitals = [p for p in candidates if p.get("feature_code") in {"PPLC", "PPLA"}]
    if len(capitals) == 1:
        return capitals
    choices = []
    for place in candidates:
        label = " · ".join(dict.fromkeys(part for part in (
            place["name"], place.get("admin2"), place.get("admin1"), place.get("country"),
        ) if part))
        choices.append({"name": label, "latitude": place["latitude"], "longitude": place["longitude"], "kind": "city"})
    return choices


def mentioned_district(text):
    found = set()
    for match in re.finditer(r"[\u3400-\u9fff]{1,28}?[区县旗]", text):
        phrase = PREFIX.sub("", match.group())
        parts = re.split(r"(?:准备去|打算去|要去|去|在|位于|到了|到|今天|明天|后天|现在|想知道|看看|查询|一下)", phrase)
        name = phrase if district_matches(phrase) else parts[-1]
        if len(name) >= 2:
            found.add(name)
    return found.pop() if len(found) == 1 else ""
