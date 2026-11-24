import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const three = path.join(process.env.MOSS_MODEL_PROJECTS || 'F:/动画logo','j20c/node_modules/three');
const {Matrix4,Vector3,Quaternion} = await import(pathToFileURL(path.join(three,'build/three.module.js')));
const {MeshoptDecoder} = await import(pathToFileURL(path.join(three,'examples/jsm/libs/meshopt_decoder.module.js')));
await MeshoptDecoder.ready;
const sha = bytes => createHash('sha256').update(bytes).digest('hex');

async function audit(file) {
  const bytes=await readFile(file), jsonBytes=bytes.readUInt32LE(12);
  const gltf=JSON.parse(bytes.toString('utf8',20,20+jsonBytes));
  const bin=bytes.subarray(28+jsonBytes), cache=new Map();
  function view(index) {
    if (cache.has(index)) return cache.get(index);
    const spec=gltf.bufferViews[index], ext=spec.extensions?.EXT_meshopt_compression;
    const result=ext ? new Uint8Array(ext.count*ext.byteStride) : bin.subarray(spec.byteOffset || 0,(spec.byteOffset || 0)+spec.byteLength);
    if (ext) MeshoptDecoder.decodeGltfBuffer(result,ext.count,ext.byteStride,
      bin.subarray(ext.byteOffset || 0,(ext.byteOffset || 0)+ext.byteLength),ext.mode,ext.filter);
    cache.set(index,result);
    return result;
  }
  function attribute(index) {
    const a=gltf.accessors[index], raw=view(a.bufferView);
    const type={5120:Int8Array,5121:Uint8Array,5122:Int16Array,5123:Uint16Array,5125:Uint32Array,5126:Float32Array}[a.componentType];
    const width={SCALAR:1,VEC2:2,VEC3:3,VEC4:4}[a.type];
    const data=new type(raw.buffer,raw.byteOffset+(a.byteOffset||0),
      Math.floor((raw.byteLength-(a.byteOffset||0))/type.BYTES_PER_ELEMENT));
    const stride=(gltf.bufferViews[a.bufferView].byteStride||width*type.BYTES_PER_ELEMENT)/type.BYTES_PER_ELEMENT;
    const norm=a.normalized ? (a.componentType===5122?32767:a.componentType===5123?65535:a.componentType===5120?127:255) : 1;
    return {data,stride,norm,count:a.count};
  }
  const groups=new Map(), transparent=[], root=gltf.scenes[gltf.scene||0];
  let primitives=0, triangles=0;
  function visit(id, parent, group) {
    const node=gltf.nodes[id];
    const local=node.matrix ? new Matrix4().fromArray(node.matrix) : new Matrix4().compose(
      new Vector3().fromArray(node.translation||[0,0,0]),new Quaternion().fromArray(node.rotation||[0,0,0,1]),
      new Vector3().fromArray(node.scale||[1,1,1]));
    const world=new Matrix4().multiplyMatrices(parent,local);
    for (const p of gltf.meshes?.[node.mesh]?.primitives||[]) {
      assert.equal(p.mode||4,4);
      const m=gltf.materials[p.material], key=group+' / '+m.name;
      if (m.alphaMode==='BLEND'||m.extensions?.KHR_materials_transmission)
        transparent.push([key,node.extras?.sourceId||node.name]);
      if (!groups.has(key)) groups.set(key,{count:0,sum:[0,0,0,0,0],min:[Infinity,Infinity,Infinity],max:[-Infinity,-Infinity,-Infinity]});
      const stats=groups.get(key), pos=attribute(p.attributes.POSITION), uv=p.attributes.TEXCOORD_0===undefined?null:attribute(p.attributes.TEXCOORD_0);
      const idx=attribute(p.indices), point=new Vector3();
      primitives++; triangles+=idx.count/3; stats.count+=idx.count;
      for (let i=0; i<idx.count; ++i) {
        const j=idx.data[i*idx.stride];
        point.fromArray(pos.data,j*pos.stride).divideScalar(pos.norm).applyMatrix4(world);
        for (let k=0; k<3; k++) {
          const v=point.getComponent(k);
          stats.min[k]=Math.min(stats.min[k],v); stats.max[k]=Math.max(stats.max[k],v); stats.sum[k]+=v;
        }
        if (uv) {stats.sum[3]+=uv.data[j*uv.stride]/uv.norm; stats.sum[4]+=uv.data[j*uv.stride+1]/uv.norm;}
      }
    }
    for (const child of node.children||[]) visit(child,world,group);
  }
  for (const child of root.nodes) visit(child,new Matrix4(),gltf.nodes[child].name);
  const imageHashes=gltf.images.map(image=>sha(view(image.bufferView))).sort();
  return {source:root.extras,primitives,triangles,groups,transparent:transparent.sort(),imageHashes};
}

const [before,after,allowedBounds='0.02']=process.argv.slice(2);
const boundsToleranceMm=Number(allowedBounds);
if (!before||!after||!Number.isFinite(boundsToleranceMm)||boundsToleranceMm<0)
  throw Error('需要两个 GLB 路径，可另给最大的各材质边界误差（毫米）。');
const one=await audit(before), two=await audit(after);
assert.deepEqual(one.source,two.source);
assert.deepEqual(one.imageHashes,two.imageHashes);
assert.deepEqual(one.transparent,two.transparent);
assert.equal(one.triangles,two.triangles);
assert.deepEqual([...one.groups.keys()].sort(),[...two.groups.keys()].sort());
let maxBoundsMm=0,maxCentroidMm=0,maxMeanUV=0,boundsGroup;
for (const [key,a] of one.groups) {
  const b=two.groups.get(key);
  assert.equal(a.count,b.count,key);
  for (let k=0;k<3;k++) {
    const difference=Math.max(Math.abs(b.min[k]-a.min[k]),Math.abs(b.max[k]-a.max[k]));
    if (difference>maxBoundsMm) {maxBoundsMm=difference; boundsGroup=key;}
    maxCentroidMm=Math.max(maxCentroidMm,Math.abs(b.sum[k]-a.sum[k])/a.count);
  }
  for (const k of [3,4]) maxMeanUV=Math.max(maxMeanUV,Math.abs(b.sum[k]-a.sum[k])/a.count);
}
const result={before:one.primitives,after:two.primitives,triangles:two.triangles,
  transparent:two.transparent.length,identicalImages:two.imageHashes.length,maxBoundsMm,boundsGroup,
  boundsToleranceMm,maxCentroidMm,maxMeanUV};
console.log(JSON.stringify(result));
assert.ok(two.primitives<one.primitives && maxBoundsMm<=boundsToleranceMm && maxCentroidMm<.01 && maxMeanUV<.0001);
