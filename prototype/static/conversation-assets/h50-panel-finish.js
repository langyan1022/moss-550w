// P10 的 H50 盲孔承面，几何定义见原 CAD 的 h50-panel-details.js（单位毫米）。
// 运行期矢量条码与 UV 纠偏工具：修复 GLB 内置糊斑条码与 6 处 V 轴倒置贴花，零篡改上游 .blend 交付契约。
const V_FLIPPED_DECALS = /^(?:独立制作标识\s*·\s*)?(?:opticalProcessor|serviceWarning|warningRed|exhaust)$/i;
const BARCODE_PATTERN = [2, 1, 1, 2, 1, 2, 3, 1, 1, 1, 2, 2, 1, 1, 2, 1, 1, 3, 1, 2, 1, 1, 2, 1, 2, 1, 3, 1, 2, 1, 1, 2, 2, 1, 1, 2, 1, 1, 1, 3, 1, 2, 1, 2, 1, 2];

function drawRoundedRect(ctx, x, y, w, h, r) {
  const radius = Math.min(r, w * 0.5, h * 0.5);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + w - radius, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
  ctx.lineTo(x + w, y + h - radius);
  ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
  ctx.lineTo(x + radius, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
  ctx.closePath();
  ctx.fill();
}

function upgradeH50BarcodeTexture(THREE, map, cache) {
  if (!map || !map.image || typeof OffscreenCanvas === 'undefined') return map;
  if (cache.has(map)) return cache.get(map);
  const img = map.image;
  const w = img.width || 0;
  const h = img.height || 0;
  if (w < 64 || h < 64) return map;

  const canvas = new OffscreenCanvas(w, h);
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, w, h);

  // Sample right end-tab region [0.885W..0.94W, 0.77H..0.96H]: confirm barcode ink exists & detect red vs mask
  const rx0 = Math.floor(w * 0.885);
  const ry0 = Math.floor(h * 0.770);
  const rw = Math.max(4, Math.floor(w * 0.055));
  const rh = Math.max(4, Math.floor(h * 0.190));
  const sample = ctx.getImageData(rx0, ry0, rw, rh).data;
  let inkPixels = 0;
  let redPixels = 0;
  for (let i = 0; i < sample.length; i += 4) {
    if (sample[i + 3] > 20) {
      inkPixels++;
      if (sample[i] > sample[i + 1] * 1.3 && sample[i] > sample[i + 2] * 1.3) redPixels++;
    }
  }
  if (inkPixels < 12) {
    cache.set(map, map);
    return map;
  }

  // Clear ONLY the old smudged potrace barcode + wavy end-tab region; keep H50 & M/H01G glyphs untouched
  const clearX = Math.floor(w * 0.494);
  const clearY = Math.floor(h * 0.758);
  const clearW = Math.ceil(w * 0.455);
  const clearH = h - clearY;
  ctx.clearRect(clearX, clearY, clearW, clearH);

  // Draw crisp pixel-snapped Code-128 vertical white bars
  const barLeft = Math.round(w * 0.502);
  const barRight = Math.round(w * 0.876);
  const barTop = Math.round(h * 0.794);
  const barHeight = Math.round(h * 0.152);
  const totalModules = BARCODE_PATTERN.reduce((acc, v) => acc + v, 0);
  const span = barRight - barLeft;
  ctx.fillStyle = '#ffffff';
  let cursor = 0;
  for (let i = 0; i < BARCODE_PATTERN.length; i++) {
    const units = BARCODE_PATTERN[i];
    const x0 = barLeft + Math.round((cursor / totalModules) * span);
    cursor += units;
    const x1 = barLeft + Math.round((cursor / totalModules) * span);
    if (i % 2 === 0 && x1 > x0) {
      ctx.fillRect(x0, barTop, x1 - x0, barHeight);
    }
  }

  // Draw crisp rounded end-tab matching material palette (#9b2d27 for color decal, #ffffff for mask)
  const tabX = Math.round(w * 0.889);
  const tabY = Math.round(h * 0.783);
  const tabW = Math.round(w * 0.043);
  const tabH = Math.round(h * 0.171);
  ctx.fillStyle = redPixels > 8 ? '#9b2d27' : '#ffffff';
  drawRoundedRect(ctx, tabX, tabY, tabW, tabH, Math.round(w * 0.011));

  const upgraded = new THREE.CanvasTexture(canvas);
  upgraded.name = `${map.name || 'h50'} (crisp vector barcode)`;
  upgraded.flipY = map.flipY;
  upgraded.colorSpace = map.colorSpace;
  upgraded.wrapS = map.wrapS;
  upgraded.wrapT = map.wrapT;
  upgraded.premultiplyAlpha = map.premultiplyAlpha;
  upgraded.anisotropy = 16;
  upgraded.needsUpdate = true;
  cache.set(map, upgraded);
  return upgraded;
}

export function stabilizePanelFinish(THREE, scene) {
  scene.updateMatrixWorld(true);
  let plate, ink;
  const barcodeCache = new WeakMap();
  scene.traverse(object => {
    if (!object.isMesh) return;
    const name=object.userData.sourceName || object.name || '';
    if (object.userData.mossGroup === 'faceplate') {
      if (name.startsWith('P10_主体正盖面板01') && object.material.map && object.material.roughnessMap) plate=object;
      if (object.userData.mossDecal && /(?:^|\s|·)h50$/i.test(name)) ink=object;
    }
    if (object.userData.mossDecal && V_FLIPPED_DECALS.test(name)) {
      const uv = object.geometry.getAttribute('uv');
      if (uv && !object.userData.decalVCorrected) {
        object.userData.decalVCorrected = true;
        for (let i = 0; i < uv.count; i++) {
          uv.setY(i, 1.0 - uv.getY(i));
        }
        uv.needsUpdate = true;
      }
    }
    if (object.userData.mossDecal && /(?:^|\s|·)h50(?:\.\d+)?$/i.test(name)) {
      const mat = object.material;
      const matName = mat?.name || '';
      const mapName = mat?.map?.name || '';
      if (/正面\s*h50|电影标识\s*h50|h50-film-mask/i.test(`${matName} ${mapName}`)) {
        mat.map = upgradeH50BarcodeTexture(THREE, mat.map, barcodeCache);
        mat.needsUpdate = true;
      }
    }
  });
  if (!plate || !ink) return 0;
  // 印层只用于确认是哪个承面。其位置、透明度及独立贴图都不参与下面的修改。
  const anchor=new THREE.Box3().setFromObject(ink,true).getCenter(new THREE.Vector3());
  const geometry=plate.geometry, pos=geometry.getAttribute('position'), uv=geometry.getAttribute('uv');
  const normal=geometry.getAttribute('normal'), worldNormal=new THREE.Matrix3().getNormalMatrix(plate.matrixWorld);
  if (!uv || !normal) return 0;
  const axis=new THREE.Vector3(-.055548940782,.063194956004,.996454069545);
  const planeMm=21.098296, corner=new THREE.Vector3(), nearest=new THREE.Vector3();
  const face=new THREE.Triangle(), vertices=[face.a,face.b,face.c];
  const faces=[], neighbors=new Map(), swatch=new THREE.Vector2();
  let seed, best=Infinity;
  const count=geometry.index?.count ?? pos.count;
  for (let i=0;i<count;i+=3) {
    let eligible=true;
    const indices=[];
    for (let j=0;j<3;j++) {
      const k=geometry.index ? geometry.index.getX(i+j) : i+j;
      indices.push(k);
      vertices[j].fromBufferAttribute(pos,k).applyMatrix4(plate.matrixWorld);
      corner.fromBufferAttribute(normal,k).applyNormalMatrix(worldNormal);
      if (Math.abs(vertices[j].dot(axis)-planeMm)>.04 || corner.dot(axis)<.995) {eligible=false; break;}
    }
    if (!eligible) continue;
    const keys=vertices.map(v=>[v.x,v.y,v.z].map(n=>Math.round(1000*n)).join('/'));
    const id=faces.length;
    faces.push({first:i,keys});
    for (const key of keys) {
      if (!neighbors.has(key)) neighbors.set(key,[]);
      neighbors.get(key).push(id);
    }
    face.closestPointToPoint(anchor,nearest);
    const distance=nearest.distanceToSquared(anchor);
    if (distance<best) {
      seed=id; best=distance;
      const coords=indices.map(k=>new THREE.Vector2().fromBufferAttribute(uv,k));
      THREE.Triangle.getInterpolation(nearest,face.a,face.b,face.c,...coords,swatch);
    }
  }
  if (seed===undefined || best>1) return 0;
  const queue=[seed], connected=new Set(queue);
  for (let i=0;i<queue.length;i++) {
    for (const key of faces[queue[i]].keys) {
      for (const other of neighbors.get(key)) if (!connected.has(other)) {connected.add(other); queue.push(other);}
    }
  }
  // 原色与金属/粗糙度均来自承面本身。连续取同一纹素，避免远端 mip 混入图集中相邻零件的颜色。
  // 使用独立面角，避免通过共享顶点改动孔壁和斜边的 UV。
  const fixed=geometry.index ? geometry.toNonIndexed() : geometry.clone();
  const texcoord=fixed.getAttribute('uv');
  for (const id of connected) {
    const i=faces[id].first;
    for (let j=0;j<3;j++) texcoord.setXY(i+j,swatch.x,swatch.y);
  }
  texcoord.needsUpdate=true;
  plate.geometry=fixed;
  return connected.size;
}

export function auditPanelDecals(THREE, scene) {
  scene.updateMatrixWorld(true);
  const v0 = new THREE.Vector3(), v1 = new THREE.Vector3(), v2 = new THREE.Vector3();
  const e1 = new THREE.Vector3(), e2 = new THREE.Vector3(), fn = new THREE.Vector3();
  const wup = new THREE.Vector3(), right = new THREE.Vector3(), up = new THREE.Vector3();
  const results = [];

  scene.traverse(obj => {
    if (!obj.isMesh || !obj.userData?.mossDecal) return;
    const geom = obj.geometry;
    const pos = geom?.getAttribute?.('position');
    const uv = geom?.getAttribute?.('uv');
    if (!pos || !uv) return;

    let duSum = 0, dvSum = 0, areaSum = 0;
    const triCount = geom.index ? Math.floor(geom.index.count / 3) : Math.floor(pos.count / 3);
    for (let t = 0; t < triCount; t++) {
      const i0 = geom.index ? geom.index.getX(t * 3) : t * 3;
      const i1 = geom.index ? geom.index.getX(t * 3 + 1) : t * 3 + 1;
      const i2 = geom.index ? geom.index.getX(t * 3 + 2) : t * 3 + 2;
      v0.fromBufferAttribute(pos, i0).applyMatrix4(obj.matrixWorld);
      v1.fromBufferAttribute(pos, i1).applyMatrix4(obj.matrixWorld);
      v2.fromBufferAttribute(pos, i2).applyMatrix4(obj.matrixWorld);
      e1.subVectors(v1, v0);
      e2.subVectors(v2, v0);
      fn.crossVectors(e1, e2);
      const area2 = fn.length();
      if (area2 < 1e-10) continue;
      fn.divideScalar(area2);
      wup.set(0, 1, 0);
      if (Math.abs(fn.y) > 0.95) wup.set(0, 0, -1);
      right.crossVectors(wup, fn).normalize();
      up.crossVectors(fn, right).normalize();

      const x1 = e1.dot(right), y1 = e1.dot(up);
      const x2 = e2.dot(right), y2 = e2.dot(up);
      const det = x1 * y2 - x2 * y1;
      if (Math.abs(det) < 1e-10) continue;
      const du1 = uv.getX(i1) - uv.getX(i0), dv1 = uv.getY(i1) - uv.getY(i0);
      const du2 = uv.getX(i2) - uv.getX(i0), dv2 = uv.getY(i2) - uv.getY(i0);
      duSum += ((du1 * y2 - du2 * y1) / det) * area2;
      dvSum += ((-dv1 * x2 + dv2 * x1) / det) * area2;
      areaSum += area2;
    }

    const map = obj.material?.map;
    const flipY = Boolean(map?.flipY);
    const duRight = areaSum > 0 ? duSum / areaSum : 0;
    const dvUp = areaSum > 0 ? dvSum / areaSum : 0;
    const imageUpSign = flipY ? dvUp : -dvUp;

    let barcode = null;
    if (map?.image && typeof map.image.getContext === 'function') {
      const img = map.image;
      const ctx = img.getContext('2d');
      if (ctx) {
        const y = Math.round(img.height * 0.87);
        const x0 = Math.round(img.width * 0.502);
        const x1 = Math.round(img.width * 0.876);
        const row = ctx.getImageData(x0, y, Math.max(1, x1 - x0), 1).data;
        let transitions = 0, midAlpha = 0, prevOn = null;
        for (let i = 0; i < row.length; i += 4) {
          const a = row[i + 3];
          if (a >= 20 && a <= 220) midAlpha++;
          const on = a > 220;
          if (prevOn !== null && on !== prevOn) transitions++;
          prevOn = on;
        }
        const tab = ctx.getImageData(Math.round(img.width * 0.91), y, 1, 1).data;
        barcode = {
          upgraded: String(map.name || '').includes('(crisp vector barcode)'),
          transitions,
          midAlpha,
          tabAlpha: tab[3],
          tabRed: tab[0] > tab[1] * 1.3 && tab[0] > tab[2] * 1.3,
        };
      }
    }

    results.push({
      name: obj.userData.sourceName || obj.name || '',
      nodeName: obj.name || '',
      mapName: map?.name || '',
      flipY,
      vCorrected: Boolean(obj.userData.decalVCorrected),
      duRight: Math.round(duRight * 1000) / 1000,
      dvUp: Math.round(dvUp * 1000) / 1000,
      imageUpSign: Math.round(imageUpSign * 1000) / 1000,
      barcode,
    });
  });

  return results;
}
