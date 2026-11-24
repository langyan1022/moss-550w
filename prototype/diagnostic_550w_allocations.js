// Counts logical WebGL storage requested by the renderer. The driver chooses padding,
// physical residency and implicit targets, so these totals are not a VRAM measurement.
function enumSizes(gl, groups) {
  return new Map(Object.entries(groups).flatMap(([size, names]) => names.split(" ")
    .filter(name => typeof gl[name] === "number").map(name => [gl[name], Number(size)])));
}

function sourceBytes(source, offset, length) {
  if (typeof source === "number") return source;
  if (!source) return 0;
  const unit = source.BYTES_PER_ELEMENT ?? 1;
  return length ? length * unit : Math.max(0, source.byteLength - (offset ?? 0) * unit);
}

export function trackWebGLAllocations(gl) {
  const sizedFormats = enumSizes(gl, {
    1:"R8 R8_SNORM R8UI R8I STENCIL_INDEX8",
    2:"RG8 RG8_SNORM RG8UI RG8I R16F R16UI R16I RGB565 RGB5_A1 RGBA4 DEPTH_COMPONENT16",
    3:"RGB8 SRGB8 RGB8_SNORM RGB8UI RGB8I DEPTH_COMPONENT24",
    4:"RGBA8 SRGB8_ALPHA8 RGBA8_SNORM RGBA8UI RGBA8I RGB10_A2 RGB10_A2UI R11F_G11F_B10F RGB9_E5 RG16F RG16UI RG16I R32F R32UI R32I DEPTH_COMPONENT32F DEPTH24_STENCIL8",
    6:"RGB16F RGB16UI RGB16I",
    8:"RGBA16F RGBA16UI RGBA16I RG32F RG32UI RG32I DEPTH32F_STENCIL8",
    12:"RGB32F RGB32UI RGB32I",
    16:"RGBA32F RGBA32UI RGBA32I",
  });
  const formatComponents = enumSizes(gl, {
    1:"ALPHA LUMINANCE RED RED_INTEGER DEPTH_COMPONENT", 2:"LUMINANCE_ALPHA RG RG_INTEGER",
    3:"RGB RGB_INTEGER", 4:"RGBA RGBA_INTEGER",
  });
  const packedTypes = enumSizes(gl, {
    2:"UNSIGNED_SHORT_5_6_5 UNSIGNED_SHORT_5_5_5_1 UNSIGNED_SHORT_4_4_4_4",
    4:"UNSIGNED_INT_2_10_10_10_REV UNSIGNED_INT_10F_11F_11F_REV UNSIGNED_INT_5_9_9_9_REV UNSIGNED_INT_24_8",
    8:"FLOAT_32_UNSIGNED_INT_24_8_REV",
  });
  const componentTypes = enumSizes(gl, {
    1:"UNSIGNED_BYTE BYTE", 2:"UNSIGNED_SHORT SHORT HALF_FLOAT", 4:"UNSIGNED_INT INT FLOAT",
  });
  const stages = ["vertex", "index", "other"];
  const liveBuffers = new Set(), liveTextures = new Set(), liveRenderbuffers = new Set();
  // Keep metadata only. A diagnostic must not retain the WebGL wrapper or upload source.
  const bufferRecords = new WeakMap(), textureRecords = new WeakMap(), renderbufferRecords = new WeakMap();
  const textureUnits = new Map();
  const buffers = new Map(), vertexArrays = new WeakMap();
  const defaultVertexArray = {element:null};
  let vertexArray = defaultVertexArray;
  let unit = gl.TEXTURE0;
  let boundRenderbuffer = null;
  let liveKnownBytes = 0, peakKnownBytes = 0;
  const operations = {}, instrumentationErrors = {};
  const unknownTextureFormats = new Set();
  const unknownRenderbufferFormats = new Set();
  const cubeFaces = Array.from({length:6}, (_, index) => gl.TEXTURE_CUBE_MAP_POSITIVE_X + index);

  function replaceBytes(previous, next) {
    liveKnownBytes += (next ?? 0) - (previous ?? 0);
    peakKnownBytes = Math.max(peakKnownBytes, liveKnownBytes);
  }

  function recordFor(records, object, initial) {
    if (!object) return null;
    let record = records.get(object);
    if (!record) {
      record = initial();
      records.set(object, record);
    }
    return record;
  }

  const bufferFor = object => recordFor(bufferRecords, object, () => ({}));
  const textureFor = object => recordFor(textureRecords, object, () => ({levels:new Map(), parameters:new Map()}));
  const renderbufferFor = object => recordFor(renderbufferRecords, object, () => ({}));
  const textureBinding = target => textureUnits.get(unit)?.get(cubeFaces.includes(target) ? gl.TEXTURE_CUBE_MAP : target);
  const bufferBinding = target => target === gl.ELEMENT_ARRAY_BUFFER ? vertexArray.element : buffers.get(target);

  function setBufferBinding(target, object) {
    const record = bufferFor(object);
    if (target === gl.ELEMENT_ARRAY_BUFFER) vertexArray.element = record;
    else buffers.set(target, record);
  }

  function hook(name, observe) {
    const original = gl[name];
    if (typeof original !== "function") return;
    gl[name] = function(...args) {
      const result = original.apply(this, args);
      try {
        operations[name] = (operations[name] ?? 0) + 1;
        observe(...args);
      } catch {
        // A measurement failure must not change the result or error state of WebGL.
        instrumentationErrors[name] = (instrumentationErrors[name] ?? 0) + 1;
      }
      return result;
    };
  }

  function deleteStorage(records, live, object) {
    const record = records.get(object);
    if (!record || record.deleted) return;
    replaceBytes(record.bytes, 0);
    record.deleted = true;
    live.delete(record);
  }

  hook("bindVertexArray", object => {
    vertexArray = recordFor(vertexArrays, object, () => ({element:null})) ?? defaultVertexArray;
  });
  hook("deleteVertexArray", object => {
    if (vertexArrays.get(object) === vertexArray) vertexArray = defaultVertexArray;
    vertexArrays.delete(object);
  });
  hook("bindBuffer", setBufferBinding);
  hook("bindBufferBase", (target, _index, object) => setBufferBinding(target, object));
  hook("bindBufferRange", (target, _index, object) => setBufferBinding(target, object));
  hook("bufferData", (target, source, _usage, offset, length) => {
    const record = bufferBinding(target);
    if (!record || record.deleted) return;
    const bytes = sourceBytes(source, offset, length);
    replaceBytes(record.bytes, bytes);
    record.bytes = bytes;
    record.role ??= target === gl.ARRAY_BUFFER ? "vertex" : target === gl.ELEMENT_ARRAY_BUFFER ? "index" : "other";
    if (target === gl.ARRAY_BUFFER) record.role = "vertex";
    if (target === gl.ELEMENT_ARRAY_BUFFER) record.role = "index";
    liveBuffers.add(record);
  });
  hook("deleteBuffer", object => deleteStorage(bufferRecords, liveBuffers, object));

  hook("activeTexture", value => { unit = value; });
  hook("bindTexture", (target, object) => {
    if (!textureUnits.has(unit)) textureUnits.set(unit, new Map());
    textureUnits.get(unit).set(target, textureFor(object));
  });
  hook("deleteTexture", object => deleteStorage(textureRecords, liveTextures, object));
  hook("framebufferTexture2D", (_target, _attachment, _face, object) => {
    if (object) textureFor(object).framebuffer = true;
  });
  hook("framebufferTextureLayer", (_target, _attachment, object) => {
    if (object) textureFor(object).framebuffer = true;
  });
  hook("texParameteri", (target, parameter, value) => {
    const record = textureBinding(target);
    if (record && !record.deleted && (parameter === gl.TEXTURE_BASE_LEVEL || parameter === gl.TEXTURE_MAX_LEVEL)) {
      record.parameters.set(parameter, value);
    }
  });

  function bytesPerPixel(internalFormat, format, type) {
    const sized = sizedFormats.get(internalFormat);
    if (sized !== undefined) return sized;
    if (!formatComponents.has(format) && internalFormat !== gl.DEPTH_STENCIL) return null;
    return packedTypes.get(type) ?? ((formatComponents.get(format) ?? 0) * (componentTypes.get(type) ?? 0) || null);
  }

  function image(target, level, internalFormat, width, height, depth, bpp, compressedBytes) {
    const record = textureBinding(target);
    if (!record || record.deleted) return;
    const size = compressedBytes ?? (bpp === null ? null : width * height * depth * bpp);
    const key = `${target}/${level}`;
    const previous = record.levels.get(key)?.bytes;
    record.levels.set(key, {width, height, depth, bpp, internalFormat, bytes:size});
    replaceBytes(previous, size);
    record.bytes = (record.bytes ?? 0) + (size ?? 0) - (previous ?? 0);
    if (size === null) unknownTextureFormats.add(internalFormat);
    liveTextures.add(record);
  }

  hook("texImage2D", (...args) => {
    const [target, level, internalFormat] = args;
    if (args.length >= 9) {
      const [, , , width, height, , format, type] = args;
      image(target, level, internalFormat, width, height, 1, bytesPerPixel(internalFormat, format, type));
    } else {
      const [, , , format, type, source] = args;
      const width = source.videoWidth || source.naturalWidth || source.displayWidth || source.width;
      const height = source.videoHeight || source.naturalHeight || source.displayHeight || source.height;
      image(target, level, internalFormat, width, height, 1, bytesPerPixel(internalFormat, format, type));
    }
  });
  hook("texImage3D", (target, level, internalFormat, width, height, depth, _border, format, type) => {
    image(target, level, internalFormat, width, height, depth, bytesPerPixel(internalFormat, format, type));
  });
  hook("copyTexImage2D", (target, level, internalFormat, _x, _y, width, height) => {
    image(target, level, internalFormat, width, height, 1, bytesPerPixel(internalFormat, internalFormat, gl.UNSIGNED_BYTE));
  });
  hook("compressedTexImage2D", (target, level, internalFormat, width, height, _border, source, offset, length) => {
    image(target, level, internalFormat, width, height, 1, null, sourceBytes(source, offset, length));
  });
  hook("compressedTexImage3D", (target, level, internalFormat, width, height, depth, _border, source, offset, length) => {
    image(target, level, internalFormat, width, height, depth, null, sourceBytes(source, offset, length));
  });

  function mipChain(target, base, last, format, width, height, depth, bpp) {
    for (let level = base; level <= last; level++) {
      image(target, level, format, width, height, depth, bpp);
      width = Math.max(1, Math.floor(width / 2));
      height = Math.max(1, Math.floor(height / 2));
      if (target === gl.TEXTURE_3D) depth = Math.max(1, Math.floor(depth / 2));
    }
  }

  function textureStorage(target, levels, format, width, height, depth = 1) {
    const record = textureBinding(target);
    if (!record || record.deleted) return;
    replaceBytes(record.bytes, 0);
    record.bytes = 0;
    record.levels.clear();
    record.immutable = true;
    const bpp = sizedFormats.get(format) ?? null;
    const faces = target === gl.TEXTURE_CUBE_MAP ? cubeFaces : [target];
    for (const face of faces) mipChain(face, 0, levels - 1, format, width, height, depth, bpp);
  }
  hook("texStorage2D", textureStorage);
  hook("texStorage3D", textureStorage);
  hook("generateMipmap", target => {
    const record = textureBinding(target);
    if (!record || record.deleted || record.immutable) return;
    const base = record.parameters.get(gl.TEXTURE_BASE_LEVEL) ?? 0;
    const max = record.parameters.get(gl.TEXTURE_MAX_LEVEL) ?? 1000;
    for (const face of target === gl.TEXTURE_CUBE_MAP ? cubeFaces : [target]) {
      const shape = record.levels.get(`${face}/${base}`);
      if (!shape || shape.bpp === null) continue;
      const largest = Math.max(shape.width, shape.height, target === gl.TEXTURE_3D ? shape.depth : 1);
      mipChain(face, base, Math.min(max, base + Math.floor(Math.log2(largest))), shape.internalFormat,
               shape.width, shape.height, shape.depth, shape.bpp);
    }
  });

  hook("bindRenderbuffer", (_target, object) => { boundRenderbuffer = renderbufferFor(object); });
  function renderbufferStorage(samples, format, width, height) {
    if (!boundRenderbuffer || boundRenderbuffer.deleted) return;
    const bpp = sizedFormats.get(format);
    const bytes = bpp === undefined ? null : Math.max(1, samples) * width * height * bpp;
    if (bytes === null) unknownRenderbufferFormats.add(format);
    replaceBytes(boundRenderbuffer.bytes, bytes);
    boundRenderbuffer.bytes = bytes;
    liveRenderbuffers.add(boundRenderbuffer);
  }
  hook("renderbufferStorage", (_target, format, width, height) => renderbufferStorage(1, format, width, height));
  hook("renderbufferStorageMultisample", (_target, samples, format, width, height) => renderbufferStorage(samples, format, width, height));
  hook("deleteRenderbuffer", object => deleteStorage(renderbufferRecords, liveRenderbuffers, object));

  function summarize(records) {
    const row = {objects:0, bytes:0, unknown:0};
    for (const record of records) {
      row.objects++;
      row.bytes += record.bytes ?? 0;
      row.unknown += record.levels ? [...record.levels.values()].filter(level => level.bytes === null).length : Number(record.bytes === null);
    }
    return row;
  }

  return () => {
    const buffers = Object.fromEntries(stages.map(role => [role, summarize([...liveBuffers].filter(record => record.role === role))]));
    const textures = {
      sampled:summarize([...liveTextures].filter(record => !record.framebuffer)),
      framebuffer:summarize([...liveTextures].filter(record => record.framebuffer)),
    };
    return {
      buffers, textures, renderbuffers:summarize(liveRenderbuffers), totalKnownBytes:liveKnownBytes, peakKnownBytes,
      unknownTextureFormats:[...unknownTextureFormats], unknownRenderbufferFormats:[...unknownRenderbufferFormats],
      operations:{...operations}, instrumentationErrors:{...instrumentationErrors},
      limits:"Logical API storage. Texture framebuffer role means ever attached. Unknown texture counts are levels. Excludes implicit garbage collection, the default framebuffer, driver padding, VRAM residency and browser CPU allocations.",
    };
  };
}
