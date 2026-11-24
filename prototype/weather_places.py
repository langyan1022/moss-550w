import csv
from collections import defaultdict
from functools import cache
from pathlib import Path


def _names(name, suffixes):
    for suffix in suffixes:
        if name.endswith(suffix) and len(name) - len(suffix) >= 2:
            return (name, name[:-len(suffix)])
    return (name,)


@cache
def _district_index():
    index = defaultdict(list)
    cities = set()
    source = Path(__file__).with_name("weather_data") / "districts.csv"
    with source.open(encoding="utf-8", newline="") as data:
        for row in csv.DictReader(data):
            district, parent, province = row["district"], row["parent"], row["province"]
            province_forms = _names(province, ("维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "省", "市"))
            parent_forms = _names(parent, ("自治州", "地区", "盟", "市"))
            cities.update(parent_forms)
            prefixes = set(province_forms) | set(parent_forms)
            if province != parent:
                prefixes.update(p + c for p in province_forms for c in parent_forms)
            district_forms = _names(district, ("新区", "自治县", "自治旗", "区", "县", "市", "旗"))
            queries = {district}
            queries.update(p + d for p in prefixes for d in district_forms)
            result = None
            if row["latitude"] and row["longitude"]:
                result = {"name": parent + district, "parent": parent, "latitude": float(row["latitude"]),
                          "longitude": float(row["longitude"])}
            for query in queries:
                index[query].append(result)
    return dict(index), cities


def district_matches(text: str) -> list[dict]:
    query = "".join(text.split())
    if not query:
        return []
    matches = _district_index()[0].get(query, ())
    if None in matches:
        return []
    return [dict(place) for place in matches]


def known_city(text):
    return text in _district_index()[1]
