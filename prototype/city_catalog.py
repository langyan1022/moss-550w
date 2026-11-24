import csv
from functools import cache
from math import cos, radians
from pathlib import Path


_DATA = Path(__file__).with_name("weather_data")


def _rows(filename):
    with (_DATA / filename).open(encoding="utf-8", newline="") as source:
        yield from csv.DictReader(source)


def _entry(identifier, province, city, district, latitude, longitude, **metadata):
    name = province if city == province or city.endswith("直辖县级行政区") else province + city
    return {"id":identifier, "name":name + district, "province":province, "city":city,
            "district":district, "latitude":float(latitude), "longitude":float(longitude), **metadata}


def _representative(districts):
    # If the catalogue has no city point, keep a real district point close to its districts' mean.
    latitude = sum(item["latitude"] for item in districts) / len(districts)
    longitude = sum(item["longitude"] for item in districts) / len(districts)
    scale = cos(radians(latitude))
    return min(districts, key=lambda item: ((item["latitude"] - latitude) ** 2 +
                                            ((item["longitude"] - longitude) * scale) ** 2))


@cache
def city_catalog():
    """Return the bundled location choices for the local settings form."""
    cities = {row["code"]:row for row in _rows("city_points.csv")}
    groups = {}
    for row in _rows("districts.csv"):
        code, province, parent = row["code"], row["province"], row["parent"]
        municipality = code[:2] in {"11", "12", "31", "50"}
        if municipality:
            city_code, city = code[:2] + "0000", parent
        elif parent == province:
            city_code, city = code[:2] + "9000", "省直辖县级行政区"
            if province.endswith("自治区"):
                city = "自治区直辖县级行政区"
        else:
            city_code, city = code[:4] + "00", parent
        districts = groups.setdefault((city_code, province, city), [])
        if row["latitude"] and row["longitude"]:
            districts.append(_entry("cn:" + code, province, city, row["district"], row["latitude"], row["longitude"]))

    domestic = []
    for (code, province, city), districts in groups.items():
        point = cities.get(code)
        if point:
            domestic.append(_entry("cn:" + code, province, city, "", point["latitude"], point["longitude"]))
        elif districts and not city.endswith("直辖县级行政区"):
            sample = _representative(districts)
            domestic.append(_entry("cn:" + code, province, city, "", sample["latitude"], sample["longitude"],
                                    representative_district=sample["district"]))
        domestic.extend(districts)

    grouped = {code for code, _, _ in groups}
    for code, point in cities.items():
        if code not in grouped:
            domestic.append(_entry("cn:" + code, point["province"], point["city"], "", point["latitude"], point["longitude"]))

    international = []
    for row in _rows("supplemental_places.csv"):
        identifier, region, city = row["id"], row["region"], row["city"]
        if identifier.startswith("cn:"):
            domestic.append(_entry(identifier, region, city, row["district"], row["latitude"], row["longitude"]))
        else:
            international.append({**_entry(identifier, region, city, "", row["latitude"], row["longitude"]),
                                  "name":city, "country":region})
    return {"domestic":domestic, "international":international}


@cache
def _by_id():
    return {entry["id"]:entry for group in city_catalog().values() for entry in group}


def selected_city(identifier):
    """Resolve a browser-supplied identifier using only the bundled points."""
    if isinstance(identifier, str) and (entry := _by_id().get(identifier)) is not None:
        return entry.copy()
