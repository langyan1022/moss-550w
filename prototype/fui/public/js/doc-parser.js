/**
 * Zero-dependency browser-native file & DOCX parser for the MOSS FUI Document Viewer.
 * Extracts text paragraphs and tables from real .docx (OOXML ZIP) files using the
 * standard Web Streams DecompressionStream('deflate-raw') + DOMParser APIs.
 */

async function inflateRaw(bytes) {
  if (typeof DecompressionStream === 'undefined') {
    throw new Error('DecompressionStream unavailable in this browser');
  }
  const ds = new DecompressionStream('deflate-raw');
  const writer = ds.writable.getWriter();
  writer.write(bytes);
  writer.close();
  const buf = await new Response(ds.readable).arrayBuffer();
  return new Uint8Array(buf);
}

/** Extract a named entry (e.g. "word/document.xml") from a ZIP ArrayBuffer. */
async function extractZipEntry(arrayBuffer, targetName) {
  const bytes = new Uint8Array(arrayBuffer);
  const view = new DataView(arrayBuffer);

  // Locate End of Central Directory (EOCD) signature 0x06054b50
  let eocd = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65558); i--) {
    if (view.getUint32(i, true) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new Error('Invalid ZIP / DOCX container');

  const cdCount = view.getUint16(eocd + 10, true);
  let pos = view.getUint32(eocd + 16, true);
  const decoder = new TextDecoder('utf-8');

  for (let i = 0; i < cdCount; i++) {
    if (view.getUint32(pos, true) !== 0x02014b50) break;
    const method = view.getUint16(pos + 10, true);
    const compSize = view.getUint32(pos + 20, true);
    const nameLen = view.getUint16(pos + 28, true);
    const extraLen = view.getUint16(pos + 30, true);
    const commentLen = view.getUint16(pos + 32, true);
    const localOffset = view.getUint32(pos + 42, true);
    const name = decoder.decode(bytes.subarray(pos + 46, pos + 46 + nameLen));
    pos += 46 + nameLen + extraLen + commentLen;

    if (name === targetName) {
      const lNameLen = view.getUint16(localOffset + 26, true);
      const lExtraLen = view.getUint16(localOffset + 28, true);
      const dataStart = localOffset + 30 + lNameLen + lExtraLen;
      const slice = bytes.subarray(dataStart, dataStart + compSize);
      if (method === 0) return decoder.decode(slice);
      if (method === 8) return decoder.decode(await inflateRaw(slice));
      throw new Error(`Unsupported ZIP compression method ${method}`);
    }
  }
  throw new Error(`Entry ${targetName} not found`);
}

/** Parse a real .docx ArrayBuffer into structured blocks (headings, paragraphs, tables). */
export async function parseDocxBuffer(arrayBuffer) {
  const xmlText = await extractZipEntry(arrayBuffer, 'word/document.xml');
  const doc = new DOMParser().parseFromString(xmlText, 'application/xml');
  const body = doc.getElementsByTagNameNS('*', 'body')[0] || doc.documentElement;
  const blocks = [];

  for (const child of body.children) {
    const local = child.localName;
    if (local === 'p') {
      const pStyle = child.getElementsByTagNameNS('*', 'pStyle')[0]?.getAttribute('w:val')
        || child.getElementsByTagNameNS('*', 'pStyle')[0]?.getAttribute('val')
        || '';
      const texts = [...child.getElementsByTagNameNS('*', 't')].map((t) => t.textContent || '');
      const line = texts.join('').trim();
      if (!line) continue;
      const isHeading = /^heading|title|标题/i.test(pStyle);
      blocks.push({ kind: isHeading ? 'heading' : 'paragraph', text: line });
    } else if (local === 'tbl') {
      const rows = [...child.getElementsByTagNameNS('*', 'tr')].map((tr) =>
        [...tr.getElementsByTagNameNS('*', 'tc')].map((tc) =>
          [...tc.getElementsByTagNameNS('*', 't')].map((t) => t.textContent || '').join('').trim(),
        ),
      ).filter((r) => r.some(Boolean));
      if (rows.length) blocks.push({ kind: 'table', rows });
    }
  }
  return blocks;
}

/** Classify a local File or URL filename into viewer modality. */
export function classifyFileViewer(filename = '', mime = '') {
  const low = `${filename}`.toLowerCase();
  const m = `${mime}`.toLowerCase();
  if (m.startsWith('image/') || /\.(png|jpe?g|webp|gif|svg|bmp|avif)$/.test(low)) return 'image';
  if (m.startsWith('audio/') || /\.(mp3|wav|ogg|flac|m4a|aac)$/.test(low)) return 'audio';
  if (m.startsWith('video/') || /\.(mp4|webm|mkv|mov|m4v)$/.test(low)) return 'video';
  if (m === 'application/pdf' || /\.pdf$/.test(low)) return 'pdf';
  if (/\.(docx|doc)$/.test(low) || m.includes('wordprocessingml') || m === 'application/msword') return 'word';
  return 'text';
}
