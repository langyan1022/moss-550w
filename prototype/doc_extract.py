"""Zero-dependency structured document text extractor for MOSS 550W (v5.5).

Supports:
  - Plain text & code: .txt, .md, .log, .json, .yaml, .yml, .py, .js, .ts, .html, .css, .sql, .xml, .ini, .toml
  - CSV / TSV: structured markdown-like table preview
  - Microsoft Word (.docx): paragraphs + tables via stdlib zipfile + ElementTree
  - Microsoft Excel (.xlsx): sheet names + cell tables via sharedStrings + worksheets XML
  - PDF (.pdf): lightweight pure-Python FlateDecode/text-operator extraction with honest fallback
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib

TEXT_EXTS = frozenset({
    ".txt", ".md", ".markdown", ".log", ".json", ".jsonl", ".yaml", ".yml",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".htm",
    ".css", ".sql", ".xml", ".ini", ".cfg", ".toml", ".ps1", ".sh", ".bat", ".env",
})
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})
SUPPORTED_DOC_EXTS = TEXT_EXTS | {".csv", ".tsv", ".docx", ".xlsx", ".pdf"}

MAX_EXTRACT_CHARS = 14000
MAX_FILE_BYTES = 15 * 1024 * 1024


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_csv(raw: bytes, *, delimiter: str = ",", max_rows: int = 120) -> tuple[str, dict]:
    text = _decode_bytes(raw)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = []
    total_rows = 0
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        total_rows += 1
        if len(rows) < max_rows:
            rows.append([cell.strip().replace("\n", " ")[:120] for cell in row[:24]])
    if not rows:
        return "(空表格)", {"kind": "csv", "rows": 0, "cols": 0}
    cols = max(len(r) for r in rows)
    lines = [" | ".join(r) for r in rows]
    if total_rows > max_rows:
        lines.append(f"…（共 {total_rows} 行，已展示前 {max_rows} 行）")
    return "\n".join(lines), {"kind": "csv", "rows": total_rows, "cols": cols}


def _extract_docx(raw: bytes) -> tuple[str, dict]:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        if "word/document.xml" not in zf.namelist():
            raise ValueError("不是有效的 .docx 文档（缺少 word/document.xml）")
        root = ET.fromstring(zf.read("word/document.xml"))
    w_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    blocks = []
    para_count = 0
    table_count = 0
    body = root.find(f"{w_ns}body")
    if body is None:
        return "(空 Word 文档)", {"kind": "docx", "paragraphs": 0, "tables": 0}
    for child in body:
        if child.tag == f"{w_ns}p":
            txt = "".join(t.text or "" for t in child.iter(f"{w_ns}t")).strip()
            if txt:
                para_count += 1
                blocks.append(txt)
        elif child.tag == f"{w_ns}tbl":
            table_count += 1
            trows = []
            for tr in child.findall(f"{w_ns}tr")[:80]:
                cells = []
                for tc in tr.findall(f"{w_ns}tc")[:20]:
                    ct = " ".join(
                        "".join(t.text or "" for t in p.iter(f"{w_ns}t")).strip()
                        for p in tc.findall(f"{w_ns}p")
                    ).strip()
                    cells.append(ct.replace("\n", " ")[:100])
                if any(cells):
                    trows.append(" | ".join(cells))
            if trows:
                blocks.append("[表格]\n" + "\n".join(trows))
    return ("\n\n".join(blocks) or "(空 Word 文档)"), {
        "kind": "docx", "paragraphs": para_count, "tables": table_count,
    }


def _col_index(cell_ref: str) -> int:
    col = 0
    for ch in cell_ref:
        if "A" <= ch <= "Z":
            col = col * 26 + (ord(ch) - ord("A") + 1)
        elif "a" <= ch <= "z":
            col = col * 26 + (ord(ch) - ord("a") + 1)
        else:
            break
    return max(0, col - 1)


def _extract_xlsx(raw: bytes, *, max_sheets: int = 6, max_rows_per_sheet: int = 80) -> tuple[str, dict]:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = set(zf.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in sroot:
                if si.tag.endswith("}si") or si.tag == "si":
                    shared.append("".join(el.text or "" for el in si.iter() if el.tag.endswith("}t") or el.tag == "t"))
        sheet_labels: list[str] = []
        if "xl/workbook.xml" in names:
            wroot = ET.fromstring(zf.read("xl/workbook.xml"))
            for sh in wroot.iter():
                if sh.tag.endswith("}sheet") or sh.tag == "sheet":
                    nm = sh.attrib.get("name")
                    if nm:
                        sheet_labels.append(nm)
        sheet_paths = sorted(
            (n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
            key=lambda p: int(re.search(r"(\d+)\.xml$", p).group(1)),
        )
        if not sheet_paths:
            raise ValueError("不是有效的 .xlsx 工作簿（缺少工作表 XML）")

        sections = []
        total_rows = 0
        for idx, spath in enumerate(sheet_paths[:max_sheets]):
            label = sheet_labels[idx] if idx < len(sheet_labels) else f"Sheet{idx + 1}"
            sroot = ET.fromstring(zf.read(spath))
            out_rows = []
            sheet_row_count = 0
            for row in sroot.iter():
                if not (row.tag.endswith("}row") or row.tag == "row"):
                    continue
                row_map: dict[int, str] = {}
                max_c = -1
                for c in row:
                    if not (c.tag.endswith("}c") or c.tag == "c"):
                        continue
                    ci = _col_index(c.attrib.get("r", "")) if c.attrib.get("r") else (max_c + 1)
                    ctype = c.attrib.get("t", "")
                    val = ""
                    if ctype == "inlineStr":
                        val = "".join(el.text or "" for el in c.iter() if el.tag.endswith("}t") or el.tag == "t")
                    else:
                        v_el = next((el for el in c if el.tag.endswith("}v") or el.tag == "v"), None)
                        raw_v = (v_el.text or "").strip() if v_el is not None else ""
                        if ctype == "s" and raw_v.isdigit():
                            si_i = int(raw_v)
                            val = shared[si_i] if 0 <= si_i < len(shared) else raw_v
                        elif ctype == "b":
                            val = "TRUE" if raw_v == "1" else "FALSE"
                        else:
                            val = raw_v
                    if val != "":
                        row_map[ci] = val.replace("\n", " ")[:100]
                        if ci > max_c:
                            max_c = ci
                if max_c >= 0:
                    sheet_row_count += 1
                    total_rows += 1
                    if len(out_rows) < max_rows_per_sheet:
                        cap = min(max_c + 1, 20)
                        out_rows.append(" | ".join(row_map.get(i, "") for i in range(cap)))
            if out_rows:
                note = f"\n…（该表共 {sheet_row_count} 行，已展示前 {max_rows_per_sheet} 行）" if sheet_row_count > max_rows_per_sheet else ""
                sections.append(f"## 工作表：{label}\n" + "\n".join(out_rows) + note)
    return ("\n\n".join(sections) or "(空 Excel 工作簿)"), {
        "kind": "xlsx", "sheets": len(sheet_paths), "rows": total_rows,
    }


def _unescape_pdf_lit(s: bytes) -> str:
    out = bytearray()
    i = 0
    n = len(s)
    while i < n:
        b = s[i]
        if b == 0x5C and i + 1 < n:
            nxt = s[i + 1]
            esc = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12,
                   ord("("): 40, ord(")"): 41, ord("\\"): 92}
            if nxt in esc:
                out.append(esc[nxt])
                i += 2
                continue
        out.append(b)
        i += 1
    if out.startswith(b"\xfe\xff"):
        return out[2:].decode("utf-16-be", errors="ignore")
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return out.decode("latin-1", errors="ignore")


def _extract_pdf(raw: bytes) -> tuple[str, dict]:
    # Pure-Python PDF text stream extractor (handles FlateDecode & uncompressed text ops;
    # scanned/CJK-CID-embedded PDFs fall back gracefully with an honest message).
    chunks: list[str] = []
    streams = list(re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S))
    for m in streams[:200]:
        blob = m.group(1)
        data = blob
        if b"BT" not in blob:
            try:
                data = zlib.decompress(blob)
            except zlib.error:
                continue
        if b"BT" not in data:
            continue
        for bt in re.findall(rb"BT(.*?)ET", data, re.S):
            for lit in re.findall(rb"\((?:\\.|[^\\()])*\)", bt):
                txt = _unescape_pdf_lit(lit[1:-1]).strip()
                if txt and sum(ch.isprintable() for ch in txt) / max(1, len(txt)) > 0.7:
                    chunks.append(txt)
            for hx in re.findall(rb"<([0-9A-Fa-f]{4,})>", bt):
                try:
                    raw_h = bytes.fromhex(hx.decode("ascii"))
                    if len(raw_h) % 2 == 0:
                        txt = raw_h.decode("utf-16-be", errors="ignore").strip()
                        if txt and sum(ch.isprintable() for ch in txt) / max(1, len(txt)) > 0.7:
                            chunks.append(txt)
                except ValueError:
                    pass
    joined = re.sub(r"[ \t]{2,}", " ", "\n".join(chunks)).strip()
    if not joined:
        joined = "（该 PDF 可能为扫描图片或自定义字体编码，纯本机未能直接提取文字；可在控制台「+ 文件」查看器中渲染，或截图后直接 Ctrl+V 粘贴让我识图解读。）"
    return joined, {"kind": "pdf", "streams": len(streams), "extracted": bool(chunks)}


def extract_document_bytes(filename: str, raw: bytes, *, max_chars: int = MAX_EXTRACT_CHARS) -> dict:
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(f"文件体积超过上限（最大 {MAX_FILE_BYTES // (1024 * 1024)} MB）")
    name = Path(filename or "document.txt").name
    ext = Path(name).suffix.lower()
    if ext == ".docx":
        text, meta = _extract_docx(raw)
    elif ext == ".xlsx":
        text, meta = _extract_xlsx(raw)
    elif ext in {".csv", ".tsv"}:
        text, meta = _extract_csv(raw, delimiter="\t" if ext == ".tsv" else ",")
    elif ext == ".pdf":
        text, meta = _extract_pdf(raw)
    elif ext in TEXT_EXTS or not ext:
        text = _decode_bytes(raw)
        meta = {"kind": ext.lstrip(".") or "text", "lines": text.count("\n") + 1}
    else:
        raise ValueError(f"暂不支持解析 {ext} 类型文件（支持 PDF / DOCX / XLSX / CSV / 文本与代码文件，或直接粘贴图片）。")

    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n…（正文较长，已截断至前 {max_chars} 字符）"
        truncated = True
    return {
        "name": name,
        "ext": ext,
        "bytes": len(raw),
        "text": text,
        "truncated": truncated,
        "meta": meta,
    }


def extract_document_path(path: Path, *, max_chars: int = MAX_EXTRACT_CHARS) -> dict:
    raw = path.read_bytes()
    return extract_document_bytes(path.name, raw, max_chars=max_chars)
