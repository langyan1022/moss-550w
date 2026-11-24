import {readFile, writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';

const assets = path.dirname(fileURLToPath(import.meta.url));
const projects = process.env.MOSS_MODEL_PROJECTS || 'F:/动画logo';
const source = path.join(projects, 'moss');
const packages = path.join(projects, 'j20c/node_modules');
const three = path.join(packages, 'three');
const {build} = await import(pathToFileURL(path.join(packages, 'esbuild/lib/main.js')).href);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const options = {
  absWorkingDir: assets, outdir: assets, write:false,
  format: 'esm', bundle: true, minify: true, charset: 'utf8', target: ['es2022'],
  legalComments: 'inline',
  alias: {
    'moss-three': path.join(three, 'build/three.module.js'),
    'moss-orbit': path.join(three, 'examples/jsm/controls/OrbitControls.js'),
    'moss-rect-light': path.join(three, 'examples/jsm/lights/RectAreaLightUniformsLib.js'),
    'moss-rgbe': path.join(three, 'examples/jsm/loaders/RGBELoader.js'),
    'moss-gltf': path.join(three, 'examples/jsm/loaders/GLTFLoader.js'),
    'moss-meshopt': path.join(three, 'examples/jsm/libs/meshopt_decoder.module.js'),
    'moss-lighting': path.join(source, 'assets/studio_small_09_1k.hdr'),
    'moss-assembly': path.join(source, 'mesh-model.js'),
    'moss-ceiling-rail': path.join(source, 'ceiling-rail.js'),
  },
  loader: {'.png':'dataurl', '.jpg':'dataurl', '.svg':'dataurl', '.hdr':'base64'},
};

const read = file => readFile(file, 'utf8');
const release = JSON.parse(await read(path.join(assets,'moss-exterior.json')));
if (!release.sourceFile || !/^[a-f0-9]{64}$/i.test(release.sourceSha256) ||
    hash(await readFile(path.join(assets,'moss-exterior.glb'))) !== release.glbSha256)
  throw Error('机身资源与发布清单不符。');
const render = await build({...options, entryPoints:{'moss-render':'moss-render.source.js'},
  define:{MOSS_EXTERIOR_SHA256:JSON.stringify(release.glbSha256),
          MOSS_SOURCE_SHA256:JSON.stringify(release.sourceSha256)}});
const view = await build({...options, entryPoints:{'moss-view':'moss-view.source.js'},
  define:{MOSS_RENDER_SHA256:JSON.stringify(hash(render.outputFiles[0].contents))}});
for (const file of [...render.outputFiles, ...view.outputFiles]) await writeFile(file.path,file.contents);

const libraries = path.join(source, 'node_modules');
const tof = JSON.parse(await read(path.join(assets,'moss-tof-motion.json')));
const notices = [
  '550W 原始 CAD：RDK550W/RDK550P-MOSS，原型复刻：小飞飞警。\nhttps://github.com/RDK550W/RDK550P-MOSS',
  await read(path.join(source,'assets/rdk550p/RDK550P-MOSS-main/LICENSE')),
  `550W 机身外观来自 ${release.sourceFile}；源文件 SHA-256 为 ${release.sourceSha256}。`,
  `TOF 镜组取自本机 ${tof.blend}。文件 SHA-256 为 ${tof.sha256}。双环动作取自第 ${tof.frames[0]} 至 ${tof.frames[1]} 帧，播放速率为每秒 ${tof.fps} 帧。`,
  'Studio Small 09 HDRI — Sergej Majboroda · Poly Haven · CC0',
  'meshoptimizer · MIT License · Copyright (C) 2016-2024 Arseny Kapoulkine',
  'three.js', await read(path.join(three,'LICENSE')),
  'polygon-clipping', await read(path.join(libraries,'polygon-clipping/LICENSE.md')),
  'earcut', await read(path.join(libraries,'earcut/LICENSE')),
  'robust-predicates', await read(path.join(libraries,'robust-predicates/LICENSE')),
  'splaytree', (await read(path.join(libraries,'splaytree/Readme.md'))).split('## License')[1],
];
await writeFile(path.join(assets, 'model-notices.txt'), notices.join('\n\n'), 'utf8');
