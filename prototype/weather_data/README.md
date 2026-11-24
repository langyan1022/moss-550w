# 区县天气地名表

`districts.csv` 随程序提供，可离线将区县地名转换为经纬度。区划名称截至 2025 年 12 月 31 日，坐标提取于 2026 年 11 月 20 日。文件含中国内地 2,845 个县级行政区的数据。每条记录包含民政区划代码、省级名称、直接上级名称、区县名称、纬度、经度及 Wikidata 实体 ID。

## 来源与许可

区划名称和代码取自 yescallop/areacodes 的 2025 年清单，作者按 CC0 1.0 发布，并注明原始资料来自民政部国家地名信息库。[固定版本清单](https://github.com/yescallop/areacodes/blob/e2c2b2e1fb4ce1fb76e879aa0b80e229adaf950c/data/2025.txt)｜[镜像来源与许可](https://github.com/yescallop/areacodes/blob/e2c2b2e1fb4ce1fb76e879aa0b80e229adaf950c/README.md)｜[国家地名信息库](https://dmfw.mca.gov.cn/XzqhVersionPublish.html)

点位出自 Wikidata 的结构化数据，按 CC0 1.0 发布。每行末列可用于打开对应实体，例如北京朝阳区和长春朝阳区分别为 Q394701 和 Q393444。[Wikidata 许可](https://www.wikidata.org/wiki/Wikidata:Licensing)｜[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/)｜[北京朝阳区](https://www.wikidata.org/wiki/Q394701)｜[长春朝阳区](https://www.wikidata.org/wiki/Q393444)

## 坐标和覆盖范围

坐标为行政区代表点，供 Open-Meteo 的 WGS84 经纬度接口选择天气格点。点位精度、来源和区县面积各不相同，天气服务返回的格点经纬度也可能有偏移。区县名称和点位不用于推断街道、用户住址或测站位置。2025 年末之后的区划变更需纳入新的年度清单。[Open-Meteo 坐标与网格说明](https://open-meteo.com/en/docs)

## 更新数据

在本目录运行 `python refresh.py` 可通过固定版本的区划清单、Wikidata 查询服务及标准库重新生成 CSV。程序按区划代码 P13971 匹配已排除撤销时间 P576 的实体，复核已提供的中文名称，读取 P625 坐标。同一实体有多个有效点位时，程序选择与其余点总距离最小的原始点位，坐标保留五位小数。找不到唯一实体时保留区划名称，坐标留空。更换区划年度时应一并更新清单的来源版本、覆盖数量和日期。

本次快照 SHA-256 为 `910066e27c72770b620f2ff2d1167969099a619c08f482da77a83a4034acc611`。

## 当前城市设置使用的数据

`city_points.csv` 补充 337 个内地城市和地区级代表点，包括四个直辖市和没有县级下属区划的东莞、中山、儋州、嘉峪关。地名和代码沿用同一份 2025 年区划清单，坐标以 Wikidata 的 P13971 与 P625 核对。同一个区划有多个坐标时，保留与其他候选点总距离最小的一条原始坐标。文件保留每个点的 Wikidata 实体 ID。

`supplemental_places.csv` 包括香港 18 区、澳门 8 个片区、台湾 22 个市县及对应的 368 个区乡镇市，以及 118 个代表性海外城市。地理坐标取自 2026 年 11 月 22 日的 GeoNames 导出数据，按 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 使用。文件保留原始 GeoNames ID。港澳台的层级按实际可选地区整理，海外城市名单和中文显示名称由本项目筛选。 [GeoNames 下载与署名](https://www.geonames.org/export/) · [原始国家、地区及城市数据](https://download.geonames.org/export/dump/)
