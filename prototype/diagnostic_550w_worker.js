const options = new URL(location.href).searchParams;
const original = new URL(options.get("entry"), location.origin);
if (original.origin !== location.origin || original.pathname !== "/conversation-assets/moss-render.js") {
  throw new Error("诊断只接受当前本机的 550W 渲染文件。");
}
const queue = [];
const hold = event => queue.push(event);
self.addEventListener("message", hold);

const clock = () => performance.timeOrigin + performance.now();
const timeline = [{name:"worker_start", time:clock()}];
const stage = (name, detail) => timeline.push({name, time:clock(), ...detail});
const trackAllocations = options.get("allocations") === "1"
  ? (await import("/qa-550w/allocations")).trackWebGLAllocations : null;
let readAllocations;
const allocationStages = [];
function allocationSample(phase) {
  if (!readAllocations) return null;
  return {phase, time:clock(), drawingBuffer:[workerContext.drawingBufferWidth, workerContext.drawingBufferHeight],
          usage:readAllocations()};
}
const resources = () => performance.getEntriesByType("resource")
  .filter(entry => /\/moss-(exterior|tof)\.glb(?:\?|$)/u.test(entry.name))
  .map(entry => ({
    name:entry.name, start:performance.timeOrigin + entry.startTime,
    responseStart:performance.timeOrigin + entry.responseStart,
    responseEnd:performance.timeOrigin + entry.responseEnd,
    encodedBytes:entry.encodedBodySize, decodedBytes:entry.decodedBodySize,
    transferBytes:entry.transferSize
  }));

const imageRequests = new Map();
let workerContext;
let firstDraw = false;
let firstFrame = false;
let done = false;
let bitmapCount = 0, bitmapFirst = null, bitmapLast = null, bitmapMs = 0;
const calls = {};
const frameGaps = [];
let previousName, previousEnd;
function recordGap(next, now) {
  if (previousEnd !== undefined && now - previousEnd > 80) {
    frameGaps.push({after:previousName, before:next, gapMs:now - previousEnd});
  }
}
const rawContext = OffscreenCanvas.prototype.getContext;
OffscreenCanvas.prototype.getContext = function(type, options) {
  const gl = rawContext.call(this, type, options);
  if (!gl || !/^webgl2?$/u.test(type) || workerContext) return gl;
  workerContext = gl;
  stage("webgl_context", {requestedPower:options?.powerPreference});
  if (trackAllocations) readAllocations = trackAllocations(gl);
  for (const name of ["compileShader", "linkProgram", "getProgramParameter",
                     "bufferData", "bufferSubData", "texImage2D", "texSubImage2D",
                     "compressedTexImage2D", "drawElements", "drawArrays", "generateMipmap",
                     "checkFramebufferStatus", "readPixels", "getBufferSubData", "fenceSync",
                     "clientWaitSync", "finish", "flush"]) {
    const nativeCall = gl[name];
    if (typeof nativeCall !== "function") continue;
    gl[name] = function(...values) {
      if (!firstFrame || done) return nativeCall.apply(this, values);
      const start = performance.now();
      recordGap(name, start);
      const result = nativeCall.apply(this, values);
      previousName = name;
      previousEnd = performance.now();
      const durations = calls[name] ??= {count:0, totalMs:0, longestMs:0};
      const delta = previousEnd - start;
      durations.count++;
      durations.totalMs += delta;
      durations.longestMs = Math.max(durations.longestMs, delta);
      if (!firstDraw && (name === "drawElements" || name === "drawArrays")) {
        firstDraw = true;
        stage("first_gl_draw");
      }
      return result;
    };
  }
  return gl;
};

const nativeBitmap = createImageBitmap;
globalThis.createImageBitmap = async function(...args) {
  const start = clock();
  if (bitmapFirst === null) bitmapFirst = start;
  const bitmap = await nativeBitmap.apply(self, args);
  bitmapLast = clock();
  bitmapCount++;
  bitmapMs += bitmapLast - start;
  return bitmap;
};

const send = postMessage.bind(self);
globalThis.postMessage = function(message, ...options) {
  if (message.type === "load-progress") {
    stage(message.stage, {loaded:message.loaded, total:message.total});
    if (trackAllocations && ["lighting", "geometry", "optics-ready", "assembly", "shaders", "frame"].includes(message.stage))
      allocationStages.push(allocationSample(message.stage));
    if (message.stage === "frame") {
      firstFrame = true;
      previousName = "frame";
      previousEnd = performance.now();
    }
  }
  if (message.type === "image") imageRequests.set(message.id, {start:clock()});
  if (message.type !== "ready") return send(message, ...options);
  recordGap("worker_ready", performance.now());
  done = true;
  stage("worker_ready");
  if (trackAllocations) allocationStages.push(allocationSample("worker_ready"));
  send(message, ...options);
  const gl = workerContext;
  const ext = gl.getExtension("WEBGL_debug_renderer_info");
  send({
    type:"model-perf-diagnostic",
    value:{
      timeline, resources:resources(),
      bitmapDecode:{count:bitmapCount, first:bitmapFirst, last:bitmapLast, summedMs:bitmapMs},
      imageRequests:[...imageRequests.values()], firstFrameGL:calls, firstFrameGaps:frameGaps,
      ...(trackAllocations ? {allocationStages, allocationCapabilities:{
        defaultAntialias:gl.getContextAttributes()?.antialias ?? null,
        multisampledRenderToTexture:gl.getSupportedExtensions()?.includes("WEBGL_multisampled_render_to_texture") ?? null,
      }} : {}),
      adapter:{
        renderer:ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
        vendor:ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
        maskedRenderer:gl.getParameter(gl.RENDERER),
        bufferSize:[gl.drawingBufferWidth,gl.drawingBufferHeight],
        software:message.software
      },
      release:message.stats.modelRelease
    }
  });
};
self.addEventListener("message", ({data}) => {
  if (data.type === "image" && imageRequests.has(data.id)) imageRequests.get(data.id).done = clock();
  if (data.type === "qa-allocation-sample" && trackAllocations)
    send({type:"qa-allocation-sample", id:data.id, value:allocationSample(data.phase)});
});

const bundle = await import(original.href);
stage("exact_bundle_imported");
self.removeEventListener("message", hold);
for (const event of queue) self.onmessage(event);

export function inspectScene() {
  return bundle.inspectScene();
}
