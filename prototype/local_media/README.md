# MOSS 本地媒体库

MOSS 和媒体工具共用 `prototype/.venv`。网页按索引中的媒体 ID 读取原文件，音频或视频由浏览器播放。后台短片段验证通过 SDL 静音解码。播放回执按会话保存在服务数据目录的 `media-playback.sqlite3`。

在 `F:\moss\agent-lab\prototype` 中可以直接查现有索引。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m local_media.media_cli status
.\.venv\Scripts\python.exe -X utf8 -m local_media.media_cli search 刀郎 --kind audio --limit 6
```

默认索引是 `local_media/data/media.sqlite3`，来源定义和媒体 ID 一起保存在库内。默认来源配置在 `local_media/sources.json`。网页可通过环境变量 `MOSS_MEDIA_DB` 使用其他索引。CLI 通过 `--db` 接受同一个文件。

按曲名点歌时，先选本机可播放的版本。本机副本不可读或网页不支持其格式时，会继续查找 NAS 上的可播版本。只列版本时仍可查看本机文件。按序号播放会核对指定条目，格式不支持时说明原因。

## 本机媒体 MCP

在对话里可以输入“列出 MCP 工具”“用 MCP 查本地音乐 西海情歌”或“通过 MCP 查看本地媒体来源”。查询回执显示名称、艺人和本机或 NAS 来源类型，源目录和文件路径留在本机索引中。此入口使用固定的只读 MCP 服务 `moss_media_readonly`。每条显式查询启动一次标准输入输出连接，返回后关闭进程。点歌和实际播放仍在常规播放器入口。MCP 工具包括 `search_media` 和 `list_sources`，使用项目锁定的 Python SDK 1.30.0。[官方 SDK 客户端示例](https://github.com/modelcontextprotocol/python-sdk/blob/main/examples/snippets/clients/stdio_client.py)

在标准启动时同步依赖后运行 `.\.venv\Scripts\python.exe -X utf8 verify_media_mcp_entry.py`，可用独立会话和复制的索引验证 MCP 协议、只读结果及进程退出。已安装测试用浏览器依赖时，传入 `--browser` 复验轻量页上的结果。

添加共享时，在自定义 JSON 配置中列出希望保留的全部来源。配置使用原来的来源 ID 和目录可以保留媒体 ID。已挂载的 NAS 可以填盘符或 UNC 路径。需要独立 SMB 身份验证的来源使用 `smb.username_env`、`smb.password_env` 指定环境变量名。下面是一个磁盘和一个共享的配置示例。

```json
[
  {"id":"music","label":"本机音乐","kind":"local","path":"G:\\音乐"},
  {"id":"nas","label":"共享音乐","kind":"nas","path":"\\\\NAS\\音乐"}
]
```

需要刷新索引时运行以下命令。扫描更新索引，原媒体文件以只读方式访问。

视频可与同名的 SRT 或 WebVTT 放在同一目录，例如 `航行记录.mp4`、`航行记录.zh-Hans.srt` 或 `航行记录.vtt`。索引也读取 MP4、M4V、MOV、MKV、WebM 中的内嵌文本字幕，包括 MOV Text、SubRip、ASS、SSA 和 WebVTT。字幕时间和正文写入本机 SQLite，三字以上的台词通过三字组合全文索引检索。首次使用字幕，或字幕增删、视频替换时，可输入“更新媒体索引”。SRT 支持 UTF-8、带字节顺序标记的 Unicode 和常见中文 Windows 编码。

输入“搜台词：收到坐标”，或“哪部电影里说过‘收到坐标’”。命中结果按本机、NAS 顺序查询，同一视频的不同时间段分别显示。点击具体一条会跳到字幕起点，到达字幕末尾时暂停。搜索和点选都会核对视频与字幕的文件版本。文件改动后，更新媒体索引并重新搜索即可使用当前时间点。能否播放取决于浏览器对视频、音频编码的支持。图形字幕与直接绘入画面的文字暂不进入字幕索引。

查视频里的招牌、标题卡或烧录字幕时，可输入“查《航行记录》画面里的文字『收到坐标』”。MOSS 在本机对指定视频每 2 秒抽取一张画面，一次最多取 6 张，用 Windows OCR 识别，再列出匹配文字及其时间。点选结果可从该画面播放约 2 秒。要查看后续片段，可输入“查《航行记录》从120秒开始画面里的文字『收到坐标』”。两张取样画面之间短暂出现的字可能漏过。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m local_media.media_cli scan --config .\my-sources.json
```

隔离共享测试在本机随机端口启动只读 SMB2 服务，使用独立索引，核对本机优先检索、NAS 命中、完整流和 HTTP Range 分段读取。

```powershell
uv sync --frozen --group browser --group media-test
.\.venv\Scripts\python.exe -X utf8 verify_media_stream.py
```
