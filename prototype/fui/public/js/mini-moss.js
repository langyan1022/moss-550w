/**
 * Left Rail Independent Optical Modules:
 *  1. Real Blender-Exported 550W Holographic 3D CAD Wireframe Twin (`#rail-mini-moss`)
 *     — Loads `/vendor/models/550w-hologram.json`, exported directly from the official
 *       550W CAD assets (`moss-exterior.glb` + `moss-tof.glb`) via Blender 5.2 LTS.
 *     — 4-pass WebGL2 Holographic CAD Renderer:
 *         Pass 1: 3D calibration turntable rings & axis grid
 *         Pass 2: Watertight voxel-remeshed Fresnel hologram depth shell (hardware Z-occlusion)
 *         Pass 3: X-ray occluded back-side CAD edges (depthFunc GREATER, dim cyan)
 *         Pass 4: Razor-sharp front CAD feature edges + crimson dual-ring TOF optical eye + core glow
 *     — Automatic HiDPI Canvas2D wireframe fallback if WebGL2 is unavailable.
 *     — Synced 1:1 with the main 550W stage's `moss-optics` (`yaw`, `pitch`, rail `x`),
 *       camera presets, and voice phase. Strictly event-driven (zero idle rAF loop).
 *  2. Independent Privacy-First Local Camera Monitor (`#rail-cam-video`)
 *     — Opt-in `getUserMedia` video feed with honest `无信号` standby / no-device fallback.
 */
import { t } from './i18n.js';

const HOLOGRAM_URL = '/vendor/models/550w-hologram.json';

// Camera framing per preset matched to moss-render.js (29° vertical FOV, normalized [-1,+1] Y-up)
const PRESET_VIEWS = {
  interview:   { yaw: 0,   pitch: -9,  dist: 4.88, target: [ 0.00, -0.02, 0.00], code: '01 MAIN'  },
  front:       { yaw: 0,   pitch: -4,  dist: 3.45, target: [-0.08, -0.18, 0.04], code: '02 FRONT' },
  optics:      { yaw: 6,   pitch: -2,  dist: 1.65, target: [-0.15, -0.14, 0.32], code: '03 LENS'  },
  instruments: { yaw: 8,   pitch: 8,   dist: 2.35, target: [-0.13, -0.45, 0.16], code: '04 PANEL' },
  right:       { yaw: -44, pitch: -6,  dist: 4.55, target: [ 0.02, -0.05, 0.00], code: '05 ARRAY' },
  whole:       { yaw: -28, pitch: -11, dist: 5.25, target: [ 0.00, -0.02, 0.00], code: '06 WHOLE' },
};

/* ── Minimal Zero-Dependency 4x4 Matrix Math ── */
function mat4Perspective(fovyRad, aspect, near, far) {
  const f = 1.0 / Math.tan(fovyRad * 0.5);
  const nf = 1.0 / (near - far);
  return new Float32Array([
    f / aspect, 0, 0, 0,
    0, f, 0, 0,
    0, 0, (far + near) * nf, -1,
    0, 0, 2 * far * near * nf, 0,
  ]);
}

function mat4LookAt(eye, target, up) {
  let zx = eye[0] - target[0];
  let zy = eye[1] - target[1];
  let zz = eye[2] - target[2];
  let len = Math.hypot(zx, zy, zz) || 1;
  zx /= len; zy /= len; zz /= len;

  let xx = up[1] * zz - up[2] * zy;
  let xy = up[2] * zx - up[0] * zz;
  let xz = up[0] * zy - up[1] * zx;
  len = Math.hypot(xx, xy, xz) || 1;
  xx /= len; xy /= len; xz /= len;

  const yx = zy * xz - zz * xy;
  const yy = zz * xx - zx * xz;
  const yz = zx * xy - zy * xx;

  return new Float32Array([
    xx, yx, zx, 0,
    xy, yy, zy, 0,
    xz, yz, zz, 0,
    -(xx * eye[0] + xy * eye[1] + xz * eye[2]),
    -(yx * eye[0] + yy * eye[1] + yz * eye[2]),
    -(zx * eye[0] + zy * eye[1] + zz * eye[2]),
    1,
  ]);
}

function mat4Mul(a, b) {
  const out = new Float32Array(16);
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      out[c * 4 + r] =
        a[0 * 4 + r] * b[c * 4 + 0] +
        a[1 * 4 + r] * b[c * 4 + 1] +
        a[2 * 4 + r] * b[c * 4 + 2] +
        a[3 * 4 + r] * b[c * 4 + 3];
    }
  }
  return out;
}

function parseHexRgb(hex, fallback) {
  const m = /^#?([0-9a-f]{6})$/i.exec((hex || '').trim());
  if (!m) return fallback;
  const n = parseInt(m[1], 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

function buildBasePlatformLines(y = -1.03) {
  const pts = [];
  const pushRing = (r, segs) => {
    for (let i = 0; i < segs; i++) {
      const a0 = (i / segs) * Math.PI * 2;
      const a1 = ((i + 1) / segs) * Math.PI * 2;
      pts.push(Math.cos(a0) * r, y, Math.sin(a0) * r);
      pts.push(Math.cos(a1) * r, y, Math.sin(a1) * r);
    }
  };
  pushRing(0.86, 48);
  pushRing(0.56, 32);
  // Radial azimuth calibration ticks
  for (let i = 0; i < 12; i++) {
    const a = (i / 12) * Math.PI * 2;
    const r0 = i % 3 === 0 ? 0.50 : 0.76;
    const r1 = 0.92;
    pts.push(Math.cos(a) * r0, y, Math.sin(a) * r0);
    pts.push(Math.cos(a) * r1, y, Math.sin(a) * r1);
  }
  // Cardinal crosshair axes
  pts.push(-1.05, y, 0, 1.05, y, 0);
  pts.push(0, y, -1.05, 0, y, 1.05);
  return new Float32Array(pts);
}

/* ── WebGL2 Shaders ── */
const SHELL_VS = `#version 300 es
precision highp float;
layout(location = 0) in vec3 aPos;
uniform mat4 uMV;
uniform mat4 uMVP;
out vec3 vViewPos;
void main() {
  vViewPos = (uMV * vec4(aPos, 1.0)).xyz;
  gl_Position = uMVP * vec4(aPos, 1.0);
}`;

const SHELL_FS = `#version 300 es
precision highp float;
in vec3 vViewPos;
uniform vec3 uBaseColor;
uniform vec3 uRimColor;
uniform float uOpacity;
out vec4 fragColor;
void main() {
  vec3 dx = dFdx(vViewPos);
  vec3 dy = dFdy(vViewPos);
  vec3 N = normalize(cross(dx, dy));
  vec3 V = normalize(-vViewPos);
  float ndv = abs(dot(N, V));
  float fresnel = pow(1.0 - ndv, 2.35);
  float scan = 0.84 + 0.16 * sin(gl_FragCoord.y * 2.2);
  vec3 col = mix(uBaseColor, uRimColor, fresnel * 0.92) * scan;
  float alpha = (uOpacity + fresnel * 0.34) * scan;
  fragColor = vec4(col, alpha);
}`;

const LINE_VS = `#version 300 es
precision highp float;
layout(location = 0) in vec3 aPos;
uniform mat4 uMV;
uniform mat4 uMVP;
out float vDepth;
void main() {
  vec4 vp = uMV * vec4(aPos, 1.0);
  vDepth = -vp.z;
  gl_Position = uMVP * vec4(aPos, 1.0);
}`;

const LINE_FS = `#version 300 es
precision highp float;
in float vDepth;
uniform vec3 uNearColor;
uniform vec3 uFarColor;
uniform float uAlpha;
uniform vec2 uDepthRange;
out vec4 fragColor;
void main() {
  float t = clamp((vDepth - uDepthRange.x) / max(0.001, uDepthRange.y - uDepthRange.x), 0.0, 1.0);
  vec3 col = mix(uNearColor, uFarColor, t);
  float a = uAlpha * mix(1.0, 0.56, t);
  fragColor = vec4(col, a);
}`;

const POINT_VS = `#version 300 es
precision highp float;
layout(location = 0) in vec3 aPos;
uniform mat4 uMVP;
uniform float uPointSize;
void main() {
  gl_Position = uMVP * vec4(aPos, 1.0);
  gl_PointSize = uPointSize;
}`;

const POINT_FS = `#version 300 es
precision highp float;
uniform vec3 uColor;
uniform float uIntensity;
out vec4 fragColor;
void main() {
  vec2 d = gl_PointCoord - vec2(0.5);
  float r = length(d) * 2.0;
  if (r > 1.0) discard;
  float core = exp(-r * r * 7.5);
  float halo = pow(1.0 - r, 2.2) * 0.55;
  vec3 col = mix(uColor, vec3(1.0, 0.92, 0.88), core * 0.72);
  fragColor = vec4(col, clamp((core + halo) * uIntensity, 0.0, 1.0));
}`;

function compileShader(gl, type, src) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(sh);
    gl.deleteShader(sh);
    throw new Error(`Shader error: ${log}`);
  }
  return sh;
}

function linkProgram(gl, vsSrc, fsSrc) {
  const vs = compileShader(gl, gl.VERTEX_SHADER, vsSrc);
  const fs = compileShader(gl, gl.FRAGMENT_SHADER, fsSrc);
  const prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    const log = gl.getProgramInfoLog(prog);
    gl.deleteProgram(prog);
    throw new Error(`Program link error: ${log}`);
  }
  return prog;
}

export function createMiniMossController({ onCycleCamera } = {}) {
  const wireCanvas = document.getElementById('rail-mini-moss');
  const wireBtn = document.getElementById('rail-mini-moss-btn');
  const poseEl = document.getElementById('rail-mini-pose');
  const anglesEl = document.getElementById('rail-mini-angles');
  const meshStatEl = document.getElementById('rail-mini-stats');

  const camVideo = document.getElementById('rail-cam-video');
  const camStandby = document.getElementById('rail-cam-standby');
  const camToggleBtn = document.getElementById('rail-cam-toggle');
  const camTileBtn = document.getElementById('rail-cam-box');
  const camStatusEl = document.getElementById('rail-cam-status');
  const camMsgEl = document.getElementById('rail-cam-msg');
  const camSubEl = document.getElementById('rail-cam-sub');

  let yawDeg = 0;
  let pitchDeg = 0;
  let railSlide = 0;
  let presetId = 'interview';
  let phase = 'standby';
  let signalLevel = 0;
  let frozen = false;

  let holoData = null; // unpacked Float32Array / Uint16Array geometry
  let glCtx = null;
  let glState = null;
  let use2DFallback = false;

  let camStream = null;
  let camState = 'idle'; // idle | requesting | live | error
  let camErrorMsg = '';

  function readToken(name, fallback) {
    const val = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return val || fallback;
  }

  function unpackHologram(raw) {
    const invQ = 1.0 / (raw.quant || 10000);
    const toF32 = (arr) => {
      const out = new Float32Array(arr.length);
      for (let i = 0; i < arr.length; i++) out[i] = arr[i] * invQ;
      return out;
    };
    const toIdx = (arr, vCount) => (vCount > 65535 ? new Uint32Array(arr) : new Uint16Array(arr));

    const extV = toF32(raw.exterior.verts);
    const extI = toIdx(raw.exterior.edges, extV.length / 3);
    const tofV = toF32(raw.tof.verts);
    const tofI = toIdx(raw.tof.edges, tofV.length / 3);
    const shellV = toF32(raw.shell.verts);
    const shellI = toIdx(raw.shell.tris, shellV.length / 3);

    return {
      extV, extI,
      tofV, tofI,
      shellV, shellI,
      anchors: raw.anchors || { tofCenter: [-0.15, -0.14, 0.37], tofRadius: 0.073 },
      edgeCount: (extI.length + tofI.length) / 2,
      triCount: shellI.length / 3,
    };
  }

  function initWebGL(canvas) {
    if (!canvas || glCtx || use2DFallback) return;
    try {
      const gl = canvas.getContext('webgl2', {
        alpha: true,
        antialias: true,
        depth: true,
        premultipliedAlpha: true,
        preserveDrawingBuffer: true,
        powerPreference: 'low-power',
      });
      if (!gl) {
        use2DFallback = true;
        return;
      }
      const shellProg = linkProgram(gl, SHELL_VS, SHELL_FS);
      const lineProg = linkProgram(gl, LINE_VS, LINE_FS);
      const pointProg = linkProgram(gl, POINT_VS, POINT_FS);

      const baseLines = buildBasePlatformLines(-1.03);
      const baseVao = gl.createVertexArray();
      const baseBuf = gl.createBuffer();
      gl.bindVertexArray(baseVao);
      gl.bindBuffer(gl.ARRAY_BUFFER, baseBuf);
      gl.bufferData(gl.ARRAY_BUFFER, baseLines, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
      gl.bindVertexArray(null);

      glCtx = gl;
      glState = {
        shellProg,
        lineProg,
        pointProg,
        uShell: {
          mv: gl.getUniformLocation(shellProg, 'uMV'),
          mvp: gl.getUniformLocation(shellProg, 'uMVP'),
          base: gl.getUniformLocation(shellProg, 'uBaseColor'),
          rim: gl.getUniformLocation(shellProg, 'uRimColor'),
          opacity: gl.getUniformLocation(shellProg, 'uOpacity'),
        },
        uLine: {
          mv: gl.getUniformLocation(lineProg, 'uMV'),
          mvp: gl.getUniformLocation(lineProg, 'uMVP'),
          near: gl.getUniformLocation(lineProg, 'uNearColor'),
          far: gl.getUniformLocation(lineProg, 'uFarColor'),
          alpha: gl.getUniformLocation(lineProg, 'uAlpha'),
          range: gl.getUniformLocation(lineProg, 'uDepthRange'),
        },
        uPoint: {
          mvp: gl.getUniformLocation(pointProg, 'uMVP'),
          size: gl.getUniformLocation(pointProg, 'uPointSize'),
          color: gl.getUniformLocation(pointProg, 'uColor'),
          intensity: gl.getUniformLocation(pointProg, 'uIntensity'),
        },
        baseVao,
        baseCount: baseLines.length / 3,
        uploaded: false,
      };
    } catch {
      glCtx = null;
      glState = null;
      use2DFallback = true;
    }
  }

  function uploadGeometryToGL() {
    if (!glCtx || !glState || !holoData || glState.uploaded) return;
    const gl = glCtx;

    const makeIndexed = (verts, indices) => {
      const vao = gl.createVertexArray();
      const vb = gl.createBuffer();
      const ib = gl.createBuffer();
      gl.bindVertexArray(vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, vb);
      gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indices, gl.STATIC_DRAW);
      gl.bindVertexArray(null);
      return {
        vao,
        count: indices.length,
        idxType: indices instanceof Uint32Array ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT,
      };
    };

    glState.shell = makeIndexed(holoData.shellV, holoData.shellI);
    glState.ext = makeIndexed(holoData.extV, holoData.extI);
    glState.tof = makeIndexed(holoData.tofV, holoData.tofI);

    // Optical core point right at normalized TOF center pushed slightly forward along optical axis
    const tc = holoData.anchors.tofCenter || [-0.15, -0.14, 0.37];
    const tf = holoData.anchors.tofForward || [-0.055, 0.063, 0.996];
    const corePt = new Float32Array([
      tc[0] + tf[0] * 0.025,
      tc[1] + tf[1] * 0.025,
      tc[2] + tf[2] * 0.025,
    ]);
    const ptVao = gl.createVertexArray();
    const ptBuf = gl.createBuffer();
    gl.bindVertexArray(ptVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, ptBuf);
    gl.bufferData(gl.ARRAY_BUFFER, corePt, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    glState.coreVao = ptVao;
    glState.uploaded = true;
  }

  function computeCameraMatrices(aspect) {
    const view = PRESET_VIEWS[presetId] || PRESET_VIEWS.interview;
    const slideShift = Math.max(-0.22, Math.min(0.22, (railSlide - 0.5) * 0.44));
    // The main renderer turns the model. Orbiting the fixed wireframe uses the inverse angles.
    const totalYaw = ((view.yaw - yawDeg * 0.85) * Math.PI) / 180;
    const totalPitch = Math.max(-52, Math.min(48, view.pitch - pitchDeg * 0.65)) * (Math.PI / 180);

    const tx = view.target[0] + slideShift * 0.35;
    const ty = view.target[1];
    const tz = view.target[2];

    const dist = view.dist;
    const cosP = Math.cos(totalPitch);
    const eye = [
      tx + Math.sin(totalYaw) * cosP * dist,
      ty - Math.sin(totalPitch) * dist,
      tz + Math.cos(totalYaw) * cosP * dist,
    ];

    const safeAspect = Math.max(0.45, aspect);
    const minAspect = (presetId === 'interview' || presetId === 'whole') ? 0.63 : 0.92;
    const zoom = Math.min(1.0, safeAspect / minAspect);
    const fovy = 2.0 * Math.atan(Math.tan((29 * Math.PI) / 360) / zoom);

    const mv = mat4LookAt(eye, [tx, ty, tz], [0, 1, 0]);
    const proj = mat4Perspective(fovy, safeAspect, 0.1, 24.0);
    const mvp = mat4Mul(proj, mv);
    return { mv, mvp, dist };
  }

  function drawWebGL(W, H, dpr) {
    const gl = glCtx;
    if (!gl || !glState) return;
    if (holoData && !glState.uploaded) uploadGeometryToGL();

    const pw = Math.max(2, Math.round(W * dpr));
    const ph = Math.max(2, Math.round(H * dpr));
    if (wireCanvas.width !== pw || wireCanvas.height !== ph) {
      wireCanvas.width = pw;
      wireCanvas.height = ph;
    }

    gl.viewport(0, 0, pw, ph);
    gl.clearColor(0, 0, 0, 0);
    gl.clearDepth(1.0);
    gl.depthMask(true);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

    if (!glState.uploaded) return;

    const { mv, mvp, dist } = computeCameraMatrices(W / H);
    const cField = parseHexRgb(readToken('--fui-field', '#072838'), [0.027, 0.157, 0.22]);
    const cData = parseHexRgb(readToken('--fui-data', '#4fd0e6'), [0.31, 0.816, 0.902]);
    const cHi = parseHexRgb(readToken('--fui-text-hi', '#f1f6f8'), [0.945, 0.965, 0.973]);
    const cAct = parseHexRgb(readToken('--fui-act', '#ff9a2e'), [1.0, 0.604, 0.18]);
    const cCrit = parseHexRgb(readToken('--fui-crit', '#ff645a'), [1.0, 0.392, 0.353]);
    const cOk = parseHexRgb(readToken('--fui-ok', '#35e1a3'), [0.208, 0.882, 0.639]);

    const nearD = Math.max(0.4, dist - 1.15);
    const farD = dist + 1.15;

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.enable(gl.DEPTH_TEST);

    // Pass 1: 3D Calibration Turntable Ring & Floor Axes
    gl.useProgram(glState.lineProg);
    gl.uniformMatrix4fv(glState.uLine.mv, false, mv);
    gl.uniformMatrix4fv(glState.uLine.mvp, false, mvp);
    gl.uniform2f(glState.uLine.range, nearD, farD);
    gl.uniform3fv(glState.uLine.near, cData);
    gl.uniform3fv(glState.uLine.far, cField);
    gl.uniform1f(glState.uLine.alpha, 0.30);
    gl.depthMask(false);
    gl.depthFunc(gl.LEQUAL);
    gl.bindVertexArray(glState.baseVao);
    gl.drawArrays(gl.LINES, 0, glState.baseCount);

    // Pass 2: Watertight Voxel-Remeshed Fresnel Hologram Shell (writes Z-buffer for true hidden-line depth!)
    gl.useProgram(glState.shellProg);
    gl.uniformMatrix4fv(glState.uShell.mv, false, mv);
    gl.uniformMatrix4fv(glState.uShell.mvp, false, mvp);
    gl.uniform3fv(glState.uShell.base, cField);
    gl.uniform3fv(glState.uShell.rim, cData);
    gl.uniform1f(glState.uShell.opacity, 0.13);
    gl.depthMask(true);
    gl.depthFunc(gl.LEQUAL);
    gl.enable(gl.POLYGON_OFFSET_FILL);
    gl.polygonOffset(1.8, 2.5);
    gl.bindVertexArray(glState.shell.vao);
    gl.drawElements(gl.TRIANGLES, glState.shell.count, glState.shell.idxType, 0);
    gl.disable(gl.POLYGON_OFFSET_FILL);

    // Pass 3: X-Ray Occluded Back-Side CAD Wireframe (behind shell -> soft holographic teal)
    gl.useProgram(glState.lineProg);
    gl.uniformMatrix4fv(glState.uLine.mv, false, mv);
    gl.uniformMatrix4fv(glState.uLine.mvp, false, mvp);
    gl.uniform2f(glState.uLine.range, nearD, farD);
    gl.depthMask(false);
    gl.depthFunc(gl.GREATER);
    gl.uniform3fv(glState.uLine.near, cData);
    gl.uniform3fv(glState.uLine.far, cField);
    gl.uniform1f(glState.uLine.alpha, 0.16);
    gl.bindVertexArray(glState.ext.vao);
    gl.drawElements(gl.LINES, glState.ext.count, glState.ext.idxType, 0);

    // Pass 4: Razor-Sharp Front CAD Wireframe (depthFunc LEQUAL, luminous cyan -> ice-white)
    gl.depthFunc(gl.LEQUAL);
    const frontNear = [
      cData[0] * 0.42 + cHi[0] * 0.58,
      cData[1] * 0.42 + cHi[1] * 0.58,
      cData[2] * 0.42 + cHi[2] * 0.58,
    ];
    gl.uniform3fv(glState.uLine.near, frontNear);
    gl.uniform3fv(glState.uLine.far, cData);
    gl.uniform1f(glState.uLine.alpha, 0.90);
    gl.bindVertexArray(glState.ext.vao);
    gl.drawElements(gl.LINES, glState.ext.count, glState.ext.idxType, 0);

    // Real Dual-Ring TOF Optical Assembly in iconic MOSS crimson (reactive to thinking/speaking phase)
    const tofCol = phase === 'thinking' ? cAct : phase === 'speaking' ? cOk : cCrit;
    gl.uniform3fv(glState.uLine.near, tofCol);
    gl.uniform3fv(glState.uLine.far, cCrit);
    gl.uniform1f(glState.uLine.alpha, 0.96);
    gl.bindVertexArray(glState.tof.vao);
    gl.drawElements(gl.LINES, glState.tof.count, glState.tof.idxType, 0);

    // Pass 5: Volumetric Optical Eye Core Glow right at normalized TOF center
    gl.useProgram(glState.pointProg);
    gl.uniformMatrix4fv(glState.uPoint.mvp, false, mvp);
    gl.uniform3fv(glState.uPoint.color, tofCol);
    const pulseBoost = phase === 'listening' ? 1.35 : phase === 'speaking' ? 1.25 : 1.0;
    const basePx = (presetId === 'optics' ? 28 : 16) * dpr * (pulseBoost + signalLevel * 0.45);
    gl.uniform1f(glState.uPoint.size, basePx);
    gl.uniform1f(glState.uPoint.intensity, Math.min(1.0, 0.78 + signalLevel * 0.22));
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
    gl.bindVertexArray(glState.coreVao);
    gl.drawArrays(gl.POINTS, 0, 1);

    gl.bindVertexArray(null);
    gl.depthMask(true);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  }

  function drawCanvas2DFallback(W, H, dpr) {
    const pw = Math.max(2, Math.round(W * dpr));
    const ph = Math.max(2, Math.round(H * dpr));
    if (wireCanvas.width !== pw || wireCanvas.height !== ph) {
      wireCanvas.width = pw;
      wireCanvas.height = ph;
    }
    const ctx = wireCanvas.getContext('2d');
    if (!ctx) return;

    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!holoData) {
      ctx.restore();
      return;
    }

    const { mvp } = computeCameraMatrices(W / H);
    const projPt = (x, y, z) => {
      const cx = mvp[0] * x + mvp[4] * y + mvp[8] * z + mvp[12];
      const cy = mvp[1] * x + mvp[5] * y + mvp[9] * z + mvp[13];
      const cz = mvp[2] * x + mvp[6] * y + mvp[10] * z + mvp[14];
      const cw = mvp[3] * x + mvp[7] * y + mvp[11] * z + mvp[15];
      if (cw <= 0.05) return null;
      return [(cx / cw * 0.5 + 0.5) * W, (0.5 - cy / cw * 0.5) * H, cz / cw];
    };

    const drawSet = (verts, idx, color, alpha, lw) => {
      ctx.strokeStyle = color;
      ctx.globalAlpha = alpha;
      ctx.lineWidth = lw;
      ctx.beginPath();
      for (let i = 0; i < idx.length; i += 2) {
        const i0 = idx[i] * 3;
        const i1 = idx[i + 1] * 3;
        const p0 = projPt(verts[i0], verts[i0 + 1], verts[i0 + 2]);
        const p1 = projPt(verts[i1], verts[i1 + 1], verts[i1 + 2]);
        if (!p0 || !p1) continue;
        ctx.moveTo(p0[0], p0[1]);
        ctx.lineTo(p1[0], p1[1]);
      }
      ctx.stroke();
    };

    const cData = readToken('--fui-data', '#4fd0e6');
    const cCrit = readToken('--fui-crit', '#ff645a');
    drawSet(holoData.extV, holoData.extI, cData, 0.52, 0.85);
    drawSet(holoData.tofV, holoData.tofI, cCrit, 0.92, 1.05);
    ctx.restore();
  }

  function updateHudReadouts() {
    const view = PRESET_VIEWS[presetId] || PRESET_VIEWS.interview;
    if (poseEl) poseEl.textContent = view.code;
    if (anglesEl) {
      const yStr = `${yawDeg > 0 ? '+' : ''}${Math.round(yawDeg)}°`;
      const pStr = `${pitchDeg > 0 ? '+' : ''}${Math.round(pitchDeg)}°`;
      anglesEl.textContent = `AZ ${yStr} · EL ${pStr}`;
    }
    if (meshStatEl && holoData) {
      const k = (holoData.edgeCount / 1000).toFixed(1);
      meshStatEl.textContent = `CAD ${k}K`;
    }
  }

  function drawWireframe() {
    if (!wireCanvas) return;
    const rect = wireCanvas.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);

    if (!glCtx && !use2DFallback) initWebGL(wireCanvas);
    if (glCtx) drawWebGL(rect.width, rect.height, dpr);
    else drawCanvas2DFallback(rect.width, rect.height, dpr);
    updateHudReadouts();
  }

  // Load real Blender-exported 550W CAD hologram asynchronously
  fetch(HOLOGRAM_URL)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((raw) => {
      holoData = unpackHologram(raw);
      drawWireframe();
    })
    .catch(() => {
      if (meshStatEl) meshStatEl.textContent = 'CAD ERR';
    });

  /* ── Local Optical Camera Controller (Opt-in, Honest No-Signal / No-Device Fallback) ── */
  function syncCameraUi() {
    const live = camState === 'live';
    const busy = camState === 'requesting';
    if (camVideo) camVideo.hidden = !live;
    if (camStandby) {
      camStandby.hidden = live;
      camStandby.dataset.camState = camState;
    }
    if (camToggleBtn) {
      camToggleBtn.setAttribute('aria-pressed', String(live));
      camToggleBtn.disabled = busy;
      camToggleBtn.textContent = live
        ? t('camfeed.stop')
        : busy
          ? t('camfeed.starting')
          : t('camfeed.start');
    }
    if (camStatusEl) {
      camStatusEl.dataset.state = live ? 'ok' : camState === 'error' ? 'crit' : 'idle';
      camStatusEl.textContent = live
        ? t('camfeed.live')
        : camState === 'error'
          ? t('camfeed.err')
          : t('camfeed.off');
    }
    if (camMsgEl) {
      camMsgEl.textContent = t('audio.no_signal');
    }
    if (camSubEl) {
      if (camState === 'error') {
        camSubEl.textContent = camErrorMsg || t('camfeed.not_found');
      } else if (busy) {
        camSubEl.textContent = t('camfeed.starting');
      } else {
        camSubEl.textContent = t('camfeed.standby');
      }
    }
  }

  function stopCamera() {
    if (camStream) {
      for (const track of camStream.getTracks()) {
        try { track.stop(); } catch { /* ignore */ }
      }
      camStream = null;
    }
    if (camVideo) camVideo.srcObject = null;
    camState = 'idle';
    camErrorMsg = '';
    syncCameraUi();
  }

  async function startCamera() {
    if (camState === 'requesting') return;
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
      camState = 'error';
      camErrorMsg = t('camfeed.unsupported');
      syncCameraUi();
      return;
    }
    camState = 'requesting';
    camErrorMsg = '';
    syncCameraUi();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 640 }, height: { ideal: 400 }, aspectRatio: { ideal: 1.6 }, facingMode: 'user' },
        audio: false,
      });
      camStream = stream;
      if (camVideo) {
        camVideo.srcObject = stream;
        await camVideo.play().catch(() => {});
      }
      const videoTrack = stream.getVideoTracks()[0];
      videoTrack?.addEventListener('ended', () => stopCamera(), { once: true });
      camState = 'live';
      syncCameraUi();
    } catch (err) {
      camState = 'error';
      const name = err?.name || '';
      if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
        camErrorMsg = t('camfeed.not_found');
      } else if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
        camErrorMsg = t('camfeed.denied');
      } else {
        camErrorMsg = t('camfeed.not_found');
      }
      syncCameraUi();
    }
  }

  function toggleCamera() {
    if (camState === 'live') stopCamera();
    else startCamera();
  }

  camToggleBtn?.addEventListener('click', toggleCamera);
  camTileBtn?.addEventListener('click', toggleCamera);
  wireBtn?.addEventListener('click', () => onCycleCamera?.());
  window.addEventListener('beforeunload', () => stopCamera());

  // Keep #rail-mini-moss-btn strictly proportional (等比) to the main 550W blueprint viewport
  const mainStageWindow = document.querySelector('.moss-stage') || document.getElementById('moss-model');
  function syncStageAspect() {
    if (!mainStageWindow || !wireBtn) return;
    const r = mainStageWindow.getBoundingClientRect();
    if (r.width > 40 && r.height > 40) {
      const ratio = Math.max(0.65, Math.min(2.4, r.width / r.height));
      wireBtn.style.setProperty('--moss-stage-aspect', ratio.toFixed(4));
    }
  }

  if (typeof ResizeObserver !== 'undefined') {
    if (mainStageWindow) {
      new ResizeObserver(() => {
        syncStageAspect();
        drawWireframe();
      }).observe(mainStageWindow);
    }
    if (wireCanvas) {
      new ResizeObserver(() => drawWireframe()).observe(wireCanvas);
    }
  }
  window.addEventListener('resize', () => {
    syncStageAspect();
    drawWireframe();
  });

  syncStageAspect();
  syncCameraUi();
  requestAnimationFrame(() => { // fui-ok: R15 — one-shot initial paint after layout
    syncStageAspect();
    drawWireframe();
  });

  return {
    updatePose({ yaw, pitch, x } = {}) {
      if (frozen) return;
      if (Number.isFinite(yaw)) yawDeg = yaw;
      if (Number.isFinite(pitch)) pitchDeg = pitch;
      if (Number.isFinite(x)) railSlide = x;
      drawWireframe();
    },
    setPreset(id) {
      if (!id || id === presetId) return;
      presetId = id;
      drawWireframe();
    },
    setPhase(nextPhase) {
      if (!nextPhase || nextPhase === phase) return;
      phase = nextPhase;
      drawWireframe();
    },
    setSignalLevel(level) {
      if (frozen) return;
      const clamped = Math.max(0, Math.min(1, Number(level) || 0));
      if (Math.abs(clamped - signalLevel) < 0.05) return;
      signalLevel = clamped;
      drawWireframe();
    },
    setFrozen(next) {
      frozen = Boolean(next);
      if (!frozen) drawWireframe();
    },
    refreshI18n() {
      syncCameraUi();
      drawWireframe();
    },
    refreshLocale() {
      syncCameraUi();
      drawWireframe();
    },
    getCameraState() {
      return camState;
    },
    getCameraStream() {
      return camState === 'live' ? camStream : null;
    },
    getHologramStats() {
      return holoData
        ? { loaded: true, edges: holoData.edgeCount, tris: holoData.triCount, renderer: glCtx ? 'webgl2' : '2d' }
        : { loaded: false, edges: 0, tris: 0, renderer: glCtx ? 'webgl2' : '2d' };
    },
  };
}
