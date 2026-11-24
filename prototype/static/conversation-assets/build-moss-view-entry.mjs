import {createHash} from 'node:crypto';
import {mkdir, readFile, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {parseArgs} from 'node:util';

const {values} = parseArgs({options: {
  source:{type:'string'}, out:{type:'string'}, dependencies:{type:'string'}, help:{type:'boolean'},
}});
if (values.help) {
  console.log('node build-moss-view-entry.mjs --source <moss-view.source.js> --out <moss-view.js> --dependencies <node_modules>');
  process.exit(0);
}
if (!values.source || !values.out || !values.dependencies) {
  throw new Error('需要提供 --source、--out 和 --dependencies。');
}

const assets = path.dirname(fileURLToPath(import.meta.url));
const source = path.resolve(values.source);
const output = path.resolve(values.out);
const packages = path.resolve(values.dependencies);
if (path.basename(output) !== 'moss-view.js' || output.toLowerCase() === source.toLowerCase()) {
  throw new Error('输出须为与源文件不同的 moss-view.js。');
}

const readJSON = async filename => JSON.parse(await readFile(filename, 'utf8'));
for (const [name, version] of [['three','0.180.0'], ['esbuild','0.25.12']]) {
  const actual = await readJSON(path.join(packages, name, 'package.json'));
  if (actual.name !== name || actual.version !== version) {
    throw new Error(`需要 ${name}@${version}。`);
  }
}

const registered = await readJSON(path.resolve(assets, '../../model-release.json'));
const worker = await readFile(path.join(assets, 'moss-render.js'));
const workerHash = createHash('sha256').update(worker).digest('hex');
if (workerHash !== registered.assets?.['moss-render.js']) {
  throw new Error('当前渲染器与已验收版本的摘要不一致。');
}

const three = path.join(packages, 'three');
const {build} = await import(pathToFileURL(path.join(packages, 'esbuild', 'lib', 'main.js')).href);
const result = await build({
  absWorkingDir:assets, entryPoints:[source], outfile:output, write:false,
  format:'esm', bundle:true, minify:true, charset:'utf8', target:['es2022'], legalComments:'inline',
  alias:{
    'moss-three':path.join(three, 'build', 'three.module.js'),
    'moss-orbit':path.join(three, 'examples', 'jsm', 'controls', 'OrbitControls.js'),
  },
  define:{MOSS_RENDER_SHA256:JSON.stringify(workerHash)},
});
await mkdir(path.dirname(output), {recursive:true});
await writeFile(output, result.outputFiles[0].contents);
console.log(output);
