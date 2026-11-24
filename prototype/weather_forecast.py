from math import isfinite
import re

from weather_location import PARTS


WEATHER_CODES = {
    0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "轻毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨", 56: "轻冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "轻冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒", 80: "轻阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪", 95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}
WEATHER_DAYS = {"今天": "today", "明天": "tomorrow", "后天": "day_after_tomorrow"}
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
DAILY_FIELDS = {"temperature_min": "temperature_2m_min", "temperature_max": "temperature_2m_max",
                "precipitation_probability": "precipitation_probability_max", "code": "weather_code",
                "wind_speed": "wind_speed_10m_max", "rain": "rain_sum", "showers": "showers_sum"}
HOURLY_FIELDS = {"temperature": "temperature_2m", "precipitation_probability": "precipitation_probability",
                 "code": "weather_code", "wind_speed": "wind_speed_10m", "rain": "rain", "showers": "showers"}
FOCUS_TERMS = {
    "下雨": re.compile(r"下雨|降雨|雨量|有雨"),
    "带伞": re.compile(r"带伞"),
    "下雪": re.compile(r"下雪"),
    "穿衣": re.compile(r"穿衣|穿什么|怎么穿"),
}


def forecast_focus(question):
    return [term for term, matcher in FOCUS_TERMS.items() if matcher.search(question)]


def _number(value):
    return value if type(value) in {int, float} and isfinite(value) else None


def _at(data, field, index):
    values = data.get(field)
    return values[index] if isinstance(values, list) and index < len(values) else None


def _row(data, index, fields):
    row = {key: number for key, raw in fields.items() if (number := _number(_at(data, raw, index))) is not None}
    if row.get("code") in WEATHER_CODES:
        row["condition"] = WEATHER_CODES[row["code"]]
    return row


def _advice(summary, question):
    advice = []
    probability = summary.get("precipitation_probability")
    code = summary.get("code")
    wet = code in RAIN_CODES or any(summary.get(key, 0) >= .1 for key in ("rain", "showers"))
    if re.search(r"下雨|降雨|带伞|雨量", question):
        if wet:
            advice.append("预计有雨")
        elif code in SNOW_CODES:
            advice.append("预报有雪")
        elif probability is not None:
            advice.append("下雨的可能性较小" if probability < 30 else "有降水可能")
        else:
            advice.append("暂时没有降雨数据")
    if "带伞" in question and (wet or probability is not None):
        if code in SNOW_CODES and not wet:
            advice.append("可以带把伞挡雪")
        else:
            advice.append("建议带伞" if wet or probability >= 40 else "一般不用带伞")
    if "下雪" in question and code is not None and "预报有雪" not in advice:
        advice.append("预计有雪" if code in SNOW_CODES else "预报暂未显示降雪")
    if re.search(r"穿衣|穿什么|怎么穿", question) and (low := summary.get("temperature_min")) is not None:
        advice.append("出门穿厚外套" if low < 10 else "早出晚归可以带件外套" if low < 19 else "轻薄衣物就够")
    return advice


def _daily_line(place, day, summary, question):
    advice = _advice(summary, question)
    details = [summary["condition"]] if summary.get("condition") else []
    low, high = summary.get("temperature_min"), summary.get("temperature_max")
    if low is not None and high is not None:
        details.append(f"最低 {low} °C，最高 {high} °C")
    if (probability := summary.get("precipitation_probability")) is not None:
        details.append(f"最高降水概率 {probability}%")
    facts = "，".join(details) or "这段时间的预报数据暂缺"
    prefix = f"{place}{day}（{summary['date']}）{summary.get('part_of_day', '')}"
    return prefix + ("，".join(advice) + "。" if advice else "") + facts + "。"


def _hourly_summary(hourly, date, part_of_day):
    start, end = PARTS[part_of_day]
    samples = []
    for index, stamp in enumerate(hourly.get("time") or []):
        if (isinstance(stamp, str) and stamp.startswith(date + "T") and stamp[11:13].isdigit()
                and start <= int(stamp[11:13]) < end and (sample := _row(hourly, index, HOURLY_FIELDS))):
            samples.append({"time": stamp, **sample})
    if not samples:
        return None
    row = {"part_of_day": part_of_day, "hourly": samples}
    for key, reducer, result_key in (
        ("temperature", min, "temperature_min"), ("temperature", max, "temperature_max"),
        ("precipitation_probability", max, "precipitation_probability"), ("wind_speed", max, "wind_speed"),
        ("rain", sum, "rain"), ("showers", sum, "showers"), ("code", max, "code"),
    ):
        values = [sample[key] for sample in samples if key in sample]
        if values:
            row[result_key] = reducer(values)
    if row.get("code") in WEATHER_CODES:
        row["condition"] = WEATHER_CODES[row["code"]]
    return row


def format_weather(forecast, place, period, part_of_day, question):
    name = place["name"]
    location_kind = place.get("kind", "city")
    context = {"city": name, "period": period, "source": "Open-Meteo", "location_kind": location_kind,
               "parent": place.get("parent")}
    if focus := forecast_focus(question):
        context["focus"] = focus
    if location_kind != "browser":
        context.update(latitude=place["latitude"], longitude=place["longitude"],
                       grid_latitude=forecast.get("latitude"), grid_longitude=forecast.get("longitude"))

    if period == "current":
        current = forecast.get("current") or {}
        stamp = current.get("time")
        if not isinstance(stamp, str):
            return f"{name}的当前天气暂时查不到。", None
        result = {"date": stamp[:10], "time": stamp}
        condition = WEATHER_CODES.get(_number(current.get("weather_code")))
        facts = [condition] if condition else []
        if condition:
            result["condition"] = condition
        for key, field, label, units in (
            ("temperature", "temperature_2m", "气温", "°C"), ("precipitation", "precipitation", "降水", "毫米"),
            ("wind_speed", "wind_speed_10m", "风速", "公里/小时"),
        ):
            if (value := _number(current.get(field))) is not None:
                result[key] = value
                facts.append(f"{label} {value} {units}")
        if not facts:
            return f"{name}的当前天气暂时查不到。", None
        return f"{name}当地 {stamp.replace('T', ' ')}，" + "，".join(facts) + "。", {**context, **result}

    daily = forecast.get("daily") or {}
    hourly = forecast.get("hourly") or {}
    rows, lines = [], []
    for index, (day, day_period) in enumerate(WEATHER_DAYS.items()):
        if period not in {day_period, "three_days"}:
            continue
        date = _at(daily, "time", index)
        if not isinstance(date, str):
            continue
        row = _hourly_summary(hourly, date, part_of_day) if part_of_day else _row(daily, index, DAILY_FIELDS)
        if not row:
            detail = f"{part_of_day}逐小时预报" if part_of_day else "预报"
            lines.append(f"{name}{day}（{date}）的{detail}暂时查不到。")
            continue
        row["date"] = date
        lines.append(_daily_line(name, day, row, question))
        rows.append(row)
    if not rows:
        return "\n".join(lines) or f"{name}的预报数据暂时查不到。", None
    context.update(rows[0])
    if period == "three_days":
        context["days"] = rows
    return "\n".join(lines), context
