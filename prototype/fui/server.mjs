#!/usr/bin/env node
/**
 * MOSS 550W FUI Standalone Server & Reverse Proxy
 *
 * Serves the standalone Wandering Earth FUI frontend from F:\moss-fui\public
 * and transparently proxies live MOSS backend routes (/api/*, /media/*, /drafts/*,
 * /conversation/runtime-log) to the untouched F:\moss backend (default 127.0.0.1:18221),
 * plus read-only passthrough for 550W 3D / opening / font assets and cached Open-Meteo
 * live site weather.
 *
 * Zero external dependencies — Node.js 18+ built-ins only.
 */
import http from 'node:http';
import https from 'node:https';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(__dirname, 'public');
const DEFAULT_MOSS_ASSETS = path.resolve(__dirname, '..', 'moss', 'agent-lab', 'prototype', 'static', 'conversation-assets');
const DEFAULT_MOSS_STATIC = path.resolve(__dirname, '..', 'moss', 'agent-lab', 'prototype', 'static');

const args = process.argv.slice(2);
function getArg(flag, fallback) {
  const idx = args.indexOf(flag);
  return idx >= 0 && idx + 1 < args.length ? args[idx + 1] : fallback;
}

const PORT = Number(getArg('--port', process.env.MOSS_FUI_PORT || 18331));
const UPSTREAM = new URL(getArg('--upstream', process.env.MOSS_UPSTREAM || 'http://127.0.0.1:18221'));
const MOSS_ASSETS_DIR = path.resolve(getArg('--moss-assets', process.env.MOSS_ASSETS_DIR || DEFAULT_MOSS_ASSETS));
const MOSS_STATIC_DIR = path.resolve(getArg('--moss-static', process.env.MOSS_STATIC_DIR || DEFAULT_MOSS_STATIC));
const OFFLINE_MODE = args.includes('--offline') || process.env.MOSS_FUI_OFFLINE === '1';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.ttf': 'font/ttf',
  '.mp4': 'video/mp4',
  '.webm': 'video/webm',
  '.wav': 'audio/wav',
  '.mp3': 'audio/mpeg',
  '.glb': 'model/gltf-binary',
  '.gltf': 'model/gltf+json',
  '.wasm': 'application/wasm',
  '.task': 'application/octet-stream',
  '.tflite': 'application/octet-stream',
  '.txt': 'text/plain; charset=utf-8',
};

const WEATHER_CACHE = new Map();
const WEATHER_TTL_MS = 10 * 60 * 1000;

function safeResolve(baseDir, reqSubpath) {
  const decoded = decodeURIComponent(reqSubpath.split('?')[0]);
  const cleaned = decoded.replace(/^\/+/, '').replace(/\\/g, '/');
  const resolved = path.resolve(baseDir, cleaned);
  if (resolved !== baseDir && !resolved.startsWith(baseDir + path.sep)) return null;
  return resolved;
}

function sendJson(res, status, obj, extraHeaders = {}) {
  const body = Buffer.from(JSON.stringify(obj), 'utf-8');
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': String(body.length),
    'Cache-Control': 'no-store',
    ...extraHeaders,
  });
  res.end(body);
}

function serveFile(req, res, filePath, cacheControl = 'no-cache') {
  fs.stat(filePath, (err, stat) => {
    if (err || !stat.isFile()) {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('Not found');
      return;
    }
    const ext = path.extname(filePath).toLowerCase();
    const contentType = MIME[ext] || 'application/octet-stream';
    const range = req.headers.range;
    if (range) {
      const m = /^bytes=(\d*)-(\d*)$/.exec(range);
      if (m) {
        const start = m[1] ? parseInt(m[1], 10) : 0;
        const end = m[2] ? Math.min(stat.size - 1, parseInt(m[2], 10)) : stat.size - 1;
        if (start <= end && start < stat.size) {
          res.writeHead(206, {
            'Content-Type': contentType,
            'Content-Range': `bytes ${start}-${end}/${stat.size}`,
            'Accept-Ranges': 'bytes',
            'Content-Length': String(end - start + 1),
            'Cache-Control': cacheControl,
          });
          if (req.method === 'HEAD') return res.end();
          fs.createReadStream(filePath, { start, end }).pipe(res);
          return;
        }
      }
    }
    res.writeHead(200, {
      'Content-Type': contentType,
      'Content-Length': String(stat.size),
      'Accept-Ranges': 'bytes',
      'Cache-Control': cacheControl,
    });
    if (req.method === 'HEAD') return res.end();
    fs.createReadStream(filePath).pipe(res);
  });
}

function proxyToUpstream(req, res) {
  const isRuntimeLog = req.url?.startsWith('/conversation/runtime-log');
  if (OFFLINE_MODE) {
    if (isRuntimeLog) {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
      });
      res.write(': offline\n\n');
      return;
    }
    return sendJson(res, 503, { ok: false, offline: true, error: 'Running in offline demo mode.' });
  }
  const upstreamHost = `${UPSTREAM.hostname}:${UPSTREAM.port || 80}`;
  const headers = { ...req.headers };
  headers.host = upstreamHost;
  if (headers.origin) headers.origin = `${UPSTREAM.protocol}//${upstreamHost}`;
  if (headers.referer) {
    try {
      const ref = new URL(headers.referer);
      headers.referer = `${UPSTREAM.protocol}//${upstreamHost}${ref.pathname}${ref.search}`;
    } catch {
      delete headers.referer;
    }
  }
  delete headers['accept-encoding']; // keep SSE / JSON uncompressed for reliable streaming

  const proxyReq = http.request(
    {
      hostname: UPSTREAM.hostname,
      port: Number(UPSTREAM.port || 80),
      path: req.url,
      method: req.method,
      headers,
    },
    (proxyRes) => {
      const respHeaders = { ...proxyRes.headers };
      const isSSE = (respHeaders['content-type'] || '').includes('text/event-stream');
      if (isSSE) {
        respHeaders['cache-control'] = 'no-cache';
        respHeaders['x-accel-buffering'] = 'no';
        delete respHeaders['content-length'];
      }
      res.writeHead(proxyRes.statusCode || 502, respHeaders);
      if (isSSE && typeof res.flushHeaders === 'function') res.flushHeaders();
      proxyRes.pipe(res);
    },
  );

  proxyReq.on('error', () => {
    if (!res.headersSent) {
      if (isRuntimeLog) {
        res.writeHead(200, {
          'Content-Type': 'text/event-stream; charset=utf-8',
          'Cache-Control': 'no-cache',
          'X-Accel-Buffering': 'no',
        });
        res.write(': offline\n\n');
        return;
      }
      sendJson(res, 503, {
        ok: false,
        offline: true,
        upstream: UPSTREAM.origin,
        error: 'MOSS backend is currently offline.',
      });
    } else {
      res.end();
    }
  });

  req.on('aborted', () => proxyReq.destroy());
  res.on('close', () => {
    if (!res.writableEnded) proxyReq.destroy();
  });
  req.pipe(proxyReq);
}

function fetchOpenMeteo(lat, lon) {
  return new Promise((resolve, reject) => {
    const url =
      `https://api.open-meteo.com/v1/forecast?latitude=${encodeURIComponent(lat)}` +
      `&longitude=${encodeURIComponent(lon)}` +
      '&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m' +
      '&hourly=temperature_2m,precipitation_probability,weather_code,wind_speed_10m' +
      '&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max' +
      '&timezone=auto&forecast_days=3';
    const req = https.get(url, { timeout: 6000 }, (resp) => {
      if (resp.statusCode !== 200) {
        resp.resume();
        return reject(new Error(`Open-Meteo HTTP ${resp.statusCode}`));
      }
      const chunks = [];
      resp.on('data', (c) => chunks.push(c));
      resp.on('end', () => {
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf-8')));
        } catch (err) {
          reject(err);
        }
      });
    });
    req.on('timeout', () => {
      req.destroy(new Error('Open-Meteo timeout'));
    });
    req.on('error', reject);
  });
}

async function handleLocalWeather(req, res, parsedUrl) {
  const lat = Number(parsedUrl.searchParams.get('lat'));
  const lon = Number(parsedUrl.searchParams.get('lon'));
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
    return sendJson(res, 400, { error: 'Invalid latitude or longitude.' });
  }
  const key = `${lat.toFixed(2)},${lon.toFixed(2)}`;
  const cached = WEATHER_CACHE.get(key);
  if (cached && Date.now() - cached.at < WEATHER_TTL_MS) {
    return sendJson(res, 200, cached.data);
  }
  if (OFFLINE_MODE) {
    return sendJson(res, 503, { error: 'Offline mode', offline: true });
  }
  try {
    const raw = await fetchOpenMeteo(lat.toFixed(4), lon.toFixed(4));
    const cur = raw.current || {};
    const daily = raw.daily || {};
    const hourly = raw.hourly || {};
    const days = (daily.time || []).map((date, i) => ({
      date,
      code: daily.weather_code?.[i],
      temperature_min: daily.temperature_2m_min?.[i],
      temperature_max: daily.temperature_2m_max?.[i],
      precipitation_probability: daily.precipitation_probability_max?.[i],
      wind_speed: daily.wind_speed_10m_max?.[i],
    }));
    const nowIso = cur.time || '';
    const startIdx = Math.max(0, (hourly.time || []).findIndex((t) => t >= nowIso));
    const hours = (hourly.time || []).slice(startIdx, startIdx + 12).map((time, idx) => {
      const i = startIdx + idx;
      return {
        time,
        code: hourly.weather_code?.[i],
        temperature: hourly.temperature_2m?.[i],
        precipitation_probability: hourly.precipitation_probability?.[i],
        wind_speed: hourly.wind_speed_10m?.[i],
      };
    });
    const payload = {
      source: 'Open-Meteo',
      latitude: raw.latitude ?? lat,
      longitude: raw.longitude ?? lon,
      timezone: raw.timezone || 'UTC',
      sampled_at: new Date().toISOString(),
      current: {
        time: cur.time,
        temperature: cur.temperature_2m,
        apparent_temperature: cur.apparent_temperature,
        humidity: cur.relative_humidity_2m,
        precipitation: cur.precipitation,
        code: cur.weather_code,
        wind_speed: cur.wind_speed_10m,
        precipitation_probability: hours[0]?.precipitation_probability ?? days[0]?.precipitation_probability ?? null,
      },
      days,
      hourly: hours,
    };
    WEATHER_CACHE.set(key, { at: Date.now(), data: payload });
    if (WEATHER_CACHE.size > 32) {
      const oldest = WEATHER_CACHE.keys().next().value;
      WEATHER_CACHE.delete(oldest);
    }
    return sendJson(res, 200, payload);
  } catch (err) {
    return sendJson(res, 503, { error: err.message || 'Weather service unavailable', offline: true });
  }
}

const server = http.createServer((req, res) => {
  const host = req.headers.host || `127.0.0.1:${PORT}`;
  const parsedUrl = new URL(req.url || '/', `http://${host}`);
  const pathname = parsedUrl.pathname;

  // Reject non-loopback hosts for local safety
  const hostname = parsedUrl.hostname;
  if (!['127.0.0.1', 'localhost', '::1'].includes(hostname)) {
    res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('Forbidden');
    return;
  }

  // FUI-local utility endpoints
  if (pathname === '/fui/config') {
    return sendJson(res, 200, {
      app: 'moss-fui-console',
      port: PORT,
      upstream: UPSTREAM.origin,
      offlineMode: OFFLINE_MODE,
      mossAssetsAvailable: fs.existsSync(MOSS_ASSETS_DIR),
    });
  }
  if (pathname === '/fui/local-weather') {
    handleLocalWeather(req, res, parsedUrl);
    return;
  }
  if (pathname === '/fui/model/selfie_segmenter.tflite') {
    const localModel = path.join(PUBLIC_DIR, 'vendor', 'models', 'selfie_segmenter.tflite');
    if (fs.existsSync(localModel)) {
      return serveFile(req, res, localModel, 'public, max-age=86400');
    }
    if (OFFLINE_MODE) {
      return sendJson(res, 404, { ok: false, error: 'Model not cached locally in offline mode.' });
    }
    const modelUrl = 'https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter/float16/latest/selfie_segmenter.tflite';
    https.get(modelUrl, { timeout: 10000 }, (resp) => {
      if (resp.statusCode !== 200) {
        resp.resume();
        return sendJson(res, 502, { ok: false, error: `Upstream HTTP ${resp.statusCode}` });
      }
      const chunks = [];
      resp.on('data', (c) => chunks.push(c));
      resp.on('end', () => {
        const buf = Buffer.concat(chunks);
        try {
          fs.mkdirSync(path.dirname(localModel), { recursive: true });
          fs.writeFileSync(localModel, buf);
        } catch { /* ignore cache write error */ }
        res.writeHead(200, {
          'Content-Type': 'application/octet-stream',
          'Content-Length': String(buf.length),
          'Cache-Control': 'public, max-age=86400',
        });
        res.end(buf);
      });
    }).on('error', () => sendJson(res, 503, { ok: false, error: 'Model fetch unavailable.' }));
    return;
  }
  if (pathname === '/fui/shutdown' && req.method === 'POST') {
    sendJson(res, 200, { stopping: true });
    setTimeout(() => server.close(() => process.exit(0)), 60);
    return;
  }

  // Proxied live MOSS backend endpoints
  if (
    pathname.startsWith('/api/') ||
    pathname.startsWith('/media/') ||
    pathname.startsWith('/drafts/') ||
    pathname === '/conversation/runtime-log'
  ) {
    return proxyToUpstream(req, res);
  }

  // Read-only passthrough for 550W 3D model, opening video, and fonts from F:\moss
  if (pathname.startsWith('/conversation-assets/')) {
    const sub = pathname.slice('/conversation-assets/'.length);
    const localAsset = safeResolve(MOSS_ASSETS_DIR, sub);
    if (localAsset && fs.existsSync(localAsset)) {
      const immutable = /\.(glb|woff2|ttf|mp4|webp|wasm|task)$/i.test(localAsset);
      return serveFile(req, res, localAsset, immutable ? 'public, max-age=3600' : 'no-cache');
    }
    return proxyToUpstream(req, res);
  }

  // Optional read-only passthrough for F:\moss static media helpers (e.g. music-ack.wav)
  if (pathname.startsWith('/moss-static/')) {
    const sub = pathname.slice('/moss-static/'.length);
    const localStatic = safeResolve(MOSS_STATIC_DIR, sub);
    if (localStatic && fs.existsSync(localStatic)) {
      return serveFile(req, res, localStatic, 'no-cache');
    }
  }

  // Standalone FUI routes
  if (pathname === '/' || pathname === '/index.html' || pathname === '/conversation') {
    return serveFile(req, res, path.join(PUBLIC_DIR, 'index.html'), 'no-cache');
  }
  if (pathname === '/status' || pathname === '/status.html') {
    return serveFile(req, res, path.join(PUBLIC_DIR, 'status.html'), 'no-cache');
  }

  const publicFile = safeResolve(PUBLIC_DIR, pathname);
  if (publicFile) {
    return serveFile(req, res, publicFile, 'no-cache');
  }
  res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
  res.end('Not found');
});

server.listen(PORT, '127.0.0.1', () => {
  const modeLabel = OFFLINE_MODE ? 'OFFLINE DEMO' : `PROXY → ${UPSTREAM.origin}`;
  console.log(`MOSS-FUI listening at http://127.0.0.1:${PORT}/ [${modeLabel}]`);
});
