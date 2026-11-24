import csv
import io
import json
from collections import defaultdict
from math import cos, radians
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


AREA_CODES = ("https://raw.githubusercontent.com/yescallop/areacodes/"
              "e2c2b2e1fb4ce1fb76e879aa0b80e229adaf950c/data/2025.txt")
QUERY = """
SELECT DISTINCT ?code ?item ?label ?simplified (geof:latitude(?point) AS ?lat) (geof:longitude(?point) AS ?lon)
WHERE {
  ?item wdt:P13971 ?code; wdt:P625 ?point .
  FILTER(STRLEN(?code) = 6 && !STRENDS(?code, "00"))
  FILTER NOT EXISTS { ?item wdt:P576 ?end }
  OPTIONAL { ?item wdt:P1448 ?label FILTER(LANG(?label) = "zh-cn") }
  OPTIONAL { ?item rdfs:label ?simplified FILTER(LANG(?simplified) = "zh-cn") }
}
"""


def fetch(url):
    request = Request(url, headers={"User-Agent": "MOSS-Weather-District-Snapshot/1.0"})
    with urlopen(request, timeout=90) as response:
        return response.read().decode("utf-8-sig")


def main():
    places = dict(line.split(maxsplit=1) for line in fetch(AREA_CODES).splitlines() if line.strip())
    url = "https://query.wikidata.org/sparql?" + urlencode({"query": QUERY, "format": "json"})
    points = defaultdict(set)
    for row in json.loads(fetch(url))["results"]["bindings"]:
        code = row["code"]["value"]
        names = {row[key]["value"] for key in ("label", "simplified") if key in row}
        if code not in places or (names and places[code] not in names):
            continue
        lat, lon = (round(float(row[key]["value"]), 5) for key in ("lat", "lon"))
        if 3 <= lat <= 55 and 73 <= lon <= 136:
            points[code].add((lat, lon, row["item"]["value"].rsplit("/", 1)[-1]))
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("code", "province", "parent", "district", "latitude", "longitude", "wikidata"))
    missing = []
    for code, name in sorted(places.items()):
        if code.endswith("00"):
            continue
        candidates = sorted(points[code])
        province = places[code[:2] + "0000"]
        parent = places.get(code[:4] + "00", province)
        if len({point[2] for point in candidates}) != 1:
            missing.append(code + " " + name)
            writer.writerow((code, province, parent, name, "", "", ""))
            continue
        # 同一实体可有多个代表点，选与其余点总距离最小的已有坐标。
        factor = cos(radians(candidates[0][0]))
        representative = min(candidates, key=lambda p: sum(
            ((lat - p[0]) ** 2 + ((lon - p[1]) * factor) ** 2) ** 0.5 for lat, lon, _ in candidates))
        writer.writerow((code, province, parent, name, *representative))
    print(f"缺少唯一坐标实体的区县 {len(missing)} 个。", ", ".join(missing))
    available = sum(not code.endswith("00") for code in places) - len(missing)
    if not available:
        raise ValueError("未取得可用的区县坐标，保留已有文件")
    target = Path(__file__).with_name("districts.csv")
    staging = target.with_suffix(".tmp")
    staging.write_bytes(output.getvalue().encode("utf-8"))
    staging.replace(target)
    print(f"已写入 {available} 个区县。")


if __name__ == "__main__":
    main()
