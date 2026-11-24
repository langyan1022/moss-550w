import * as THREE from 'moss-three';
import {RectAreaLightUniformsLib} from 'moss-rect-light';
import {RGBELoader} from 'moss-rgbe';
import {GLTFLoader} from 'moss-gltf';
import {MeshoptDecoder} from 'moss-meshopt';
import {createMoss} from 'moss-assembly';
import studioHDR from 'moss-lighting';
import tofMotion from './moss-tof-motion.json';
import {createInterviewRail} from './interview-rail.source.js';
import {fitMossCamera} from './moss-projection.source.js';
import {createOverlayTravel} from './overlay-travel.js';
import {createStartupSweep} from './startup-motion.js';
import {createIdleGaze} from './idle-gaze.js';
import {installExterior} from './moss-exterior.source.js';
import {auditPanelDecals} from './h50-panel-finish.js';
import {useJointBaseLevel} from './joint-mr-sampler.js';

const bandEdges = [85, 190, 370, 770, 1500, 3100, 7000];
const bands = new Float32Array(6);
let source = 'idle';
let phase = 'standby';
let firstVoice = false;
// 肩轴、肘轴和腕轴的角度取自模型的实际运动范围。滑台行程以场景单位计。
const interviewPoses = {
  standby:  {yaw:0, arm:0, pitch:0, travel:0},
  listen:   {yaw:-11, arm:4, pitch:17, travel:-0.18},
  think:    {yaw:-9, arm:3, pitch:13, travel:0.07},
  speak:    {yaw:-13, arm:5, pitch:19, travel:0.21}
};
let level = 0;
let audioActivity=0;
const inputs = bands.slice();
const pendingImages = new Map();
let nextImage = 0;
let status = {width:1,height:1,dpr:1,visible:true};
let setCamera, setView, setTheme, resize;
let manualOrbit=false;
let overlayRects=[], layoutDirty=true, wakeLayout, contextLost=false;
let shoulderTurn=0, manualPitch=0, moveJoints, slideRail;
let beginStartup, stopStartup;
const idleGaze=createIdleGaze();
const logs=[];
let appendRuntimeLog;
let setOledPreset;
let currentOledPreset='mono-white';
export let inspectScene;

function signal(detail) {
  const changed=phase!==detail.phase;
  source = detail.source;
  firstVoice = Boolean(detail.firstVoice);
  phase = detail.phase || 'standby';
  if (changed) {
    idleGaze.reset();
    wakeLayout?.();
  }
  level = THREE.MathUtils.clamp(detail.level || 0, 0, 1);
  const {bins} = detail;
  const hzPerBin = (detail.sampleRate || 48000) / (2 * bins.length);
  for (let i = 0; i < inputs.length; i++) {
    const lo = Math.floor(bandEdges[i] / hzPerBin);
    const hi = Math.min(bins.length, Math.max(lo + 1, Math.ceil(bandEdges[i + 1] / hzPerBin)));
    let power = 0;
    for (let j = lo; j < hi; j++) power += (bins[j] / 255) ** 2;
    inputs[i] = source === 'idle' || hi <= lo ? 0 : Math.sqrt(power / (hi - lo));
  }
  audioActivity=source==='idle' ? 0 : typeof detail.dbfs==='number'
    ? THREE.MathUtils.smoothstep(detail.dbfs,-55,-41)
    : THREE.MathUtils.smoothstep(Math.max(level,Math.max(...inputs)*0.7),0.055,0.17);
}

function loadImage(url) {
  const id = nextImage++;
  const result = new Promise((resolve,reject) => pendingImages.set(id,{resolve,reject}));
  postMessage({type:'image',id,url});
  return result;
}

async function setup(canvas) {
  const loadStage = (stage, loaded, total) => postMessage({type:'load-progress',stage,loaded,total});
  loadStage('graphics');
  const renderer = new THREE.WebGLRenderer({canvas, antialias: true, alpha: true, powerPreference: 'low-power'});
  let frameTimer;
  canvas.addEventListener?.('webglcontextlost', event => {
    event.preventDefault();
    contextLost = true;
    clearTimeout(frameTimer);
    frameTimer = 0;
  });
  canvas.addEventListener?.('webglcontextrestored', () => {
    contextLost = false;
    last = performance.now();
    wakeLayout?.();
  });
  RectAreaLightUniformsLib.init();
  const gl = renderer.getContext();
  const extension = gl.getExtension('WEBGL_debug_renderer_info');
  const adapter = extension ? gl.getParameter(extension.UNMASKED_RENDERER_WEBGL) : '';
  const software = /swiftshader|llvmpipe|software|microsoft basic/i.test(adapter);
  const maxBufferSide = gl.getParameter(gl.MAX_RENDERBUFFER_SIZE);
  // 硬件按设备像素绘制，最多分配一幅 4K 画面，给本机语音合成留出显存。
  const pixelBudget = software ? 185000 : 3840 * 2160;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 0.96;
  const scene = new THREE.Scene();
  loadStage('lighting');
  const hdr = await new RGBELoader().loadAsync('data:application/octet-stream;base64,' + studioHDR);
  if (software) {
    const {width,height,data} = hdr.image;
    const small = new Uint16Array(width * height / 16 * 4);
    let dest = 0;
    for (let y = 0; y < height; y+=4) for (let x = 0; x < width; x+=4) {
      const pixel = (y*width+x)*4;
      small.set(data.subarray(pixel,pixel+4),dest); dest+=4;
    }
    hdr.image = {data:small,width:width/4,height:height/4};
  }
  hdr.mapping = THREE.EquirectangularReflectionMapping;
  const pmrem = new THREE.PMREMGenerator(renderer);
  const environment = pmrem.fromEquirectangular(hdr);
  scene.environment = environment.texture;
  scene.environmentIntensity = 0.62;
  scene.environmentRotation.y = 1.05;
  hdr.dispose();
  pmrem.dispose();
  function light(color, brightness, x, y, z) {
    const lamp = new THREE.DirectionalLight(color, brightness);
    lamp.position.set(x, y, z);
    scene.add(lamp);
    return lamp;
  }
  light(0xffeddf, 1.85, -6, 8, 8);
  light(0xc4d5ed, 0.62, 5, 4, 3);
  const rim = light(0xff766d, 1.3, 4, 3, -5);
  scene.add(new THREE.HemisphereLight(0xcbd1d9, 0x514c49, 0.27));
  setTheme = color => rim.color.set(color);
  if (status.theme) setTheme(status.theme);

  scene.fog = new THREE.FogExp2(0x19191e,0.026);
  const createCanvas = () => new OffscreenCanvas(1,1);
  loadStage('geometry');
  function reportRead(stage) {
    let previous = -Infinity;
    return event => {
      const total = event.lengthComputable && event.loaded <= event.total ? event.total : 0;
      if (event.loaded - previous < 65536 && event.loaded !== total) return;
      previous = event.loaded;
      loadStage(stage,event.loaded,total);
    };
  }
  const exteriorURL = new URL('./moss-exterior.glb',import.meta.url);
  exteriorURL.searchParams.set('v',MOSS_EXTERIOR_SHA256);
  const [model,{scene:tof},{scene:exterior}] = await Promise.all([
    createMoss({createCanvas,decalOptions:{createCanvas,loadImage}}),
    new GLTFLoader().loadAsync(new URL('./moss-tof.glb',import.meta.url).href,reportRead('optics')).then(result => {
      loadStage('optics-ready');
      return result;
    }),
    new GLTFLoader().setMeshoptDecoder(MeshoptDecoder).loadAsync(exteriorURL.href,reportRead('exterior')),
  ]);
  loadStage('assembly');
  const rail = createInterviewRail(THREE, model);
  model.meshes[17].visible=false; // 原装配顶座上方的安装卡扣。
  scene.add(rail.root);
  const rig = model.mechanisms;
  const outer=tof.getObjectByProperty('name','front-outer-ring');
  const inner=tof.getObjectByProperty('name','front-inner-ring');
  const fixed=tof.getObjectByProperty('name','fixed-optical-center');
  const emitter=fixed?.children.find(object=>object.userData.display==='main-optic');
  const frontWindow=fixed?.children.find(object=>object.userData.coatedWindow);
  if (!outer || !inner || !emitter?.material.emissiveMap || !frontWindow) throw Error('双环镜组的几何或烘焙光源缺失');
  // Blender 保留了原 Three 组件的导出编号。复用对应零件的阳极氧化层、车削纹和铜色表面。
  const original=[];
  rig.rotor.traverseVisible(object=>original.push(object));
  const sourceRoot=Number(tofMotion.sourceRoot.slice(5));
  tof.traverse(object=>{
    if(!object.isMesh) return;
    const sourceObject=original[Number(object.userData.sourceId.slice(5))-sourceRoot];
    if(!sourceObject?.isMesh) throw Error('找不到光学零件材质 '+object.userData.sourceId);
    object.material=sourceObject.material;
    if(object.userData.sourceName==='Stepped machined lens retainer') {
      object.material=object.material.clone();
      object.material.roughness=Math.max(object.material.roughness,0.36);
      object.material.specularIntensity=Math.min(object.material.specularIntensity,0.32);
    }
  });
  // 原始发光面在光圈后方。亮芯沿径向过渡到暗红，再在镜头内部淡出。
  const dieFace=new OffscreenCanvas(512,512);
  const ink=dieFace.getContext('2d');
  ink.fillStyle='#000000';
  ink.fillRect(0,0,512,512);
  const dieRadius=94;
  const die=ink.createRadialGradient(256,256,0,256,256,dieRadius);
  for(const [radius,color] of [[0,'#ff9270'],[.12,'#ff6549'],[.26,'#fc3525'],[.40,'#ec2419'],
      [.56,'#be130a'],[.71,'#690702'],[.84,'#2c0301'],[1,'#000000']]) {
    die.addColorStop(radius,color);
  }
  ink.fillStyle=die;
  ink.fillRect(256-dieRadius,256-dieRadius,dieRadius*2,dieRadius*2);
  const dieTexture=new THREE.CanvasTexture(dieFace);
  dieTexture.colorSpace=THREE.SRGBColorSpace;
  emitter.material=new THREE.MeshPhysicalMaterial({
    color:0x000000, emissive:0xffffff, emissiveMap:dieTexture, specularIntensity:0, roughness:1,
  });
  frontWindow.material=frontWindow.material.clone();
  // 直接把 Blender 的三组零件安装在原网页模型的实体镜座，外部导槽继续固定。
  tof.position.copy(rig.rotor.position);
  rig.rotor.removeFromParent();
  rig.optical.add(tof);
  frontWindow.geometry.computeBoundingSphere();
  // 低反射镀膜直接透出后方实体灯芯，浅弧面保留一层镜面反射。
  frontWindow.material.transmission=0;
  frontWindow.material.transparent=true;
  frontWindow.material.color.setHex(0x101822);
  frontWindow.material.opacity=0.075;
  frontWindow.material.roughness=0.035;
  frontWindow.material.specularIntensity=0.42;
  frontWindow.material.depthWrite=false;
  emitter.material.toneMapped=false;
  // 灯芯在整机正面只有几像素宽。让原封装透镜呈现实际透出的红光，保留六颗芯片之间的暗缝。
  for (const led of rig.auxLEDs) {
    led.emitter.scale.set(1.35,1.25,1);
    const lens = led.lens.material = led.lens.material.clone();
    lens.emissive.setHex(0xff350b);
    lens.toneMapped = false;
    lens.opacity = 0.32;
  }
  const state = {powered: true, yaw: 0, arm: 0, pitch: 0, iris: 0.63, fan: true, scan: true};
  const modelRelease = installExterior(THREE,model,tof,exterior,MOSS_EXTERIOR_SHA256,MOSS_SOURCE_SHA256);
  model.update(0, 0, state);
  model.root.updateMatrixWorld(true);
  const camera = new THREE.PerspectiveCamera(29, 1, 0.025, 100);
  let viewChanged = true;
  const stats={meshes:0,triangles:0};
  const adjustedJointMaterials=new Set();
  model.root.traverseVisible(object => {
    if (!object.isMesh || !object.layers.isEnabled(0)) return;
    stats.meshes++;
    stats.triangles+=(object.geometry.index || object.geometry.attributes.position).count/3;
    for (const material of Array.isArray(object.material) ? object.material : [object.material]) {
      useJointBaseLevel(material,THREE.LinearFilter,adjustedJointMaterials);
      if (material.transmission > 0 && (software || material.name==='H50 / optical cover glass')) {
        material.transmission=0;
        material.opacity=material.name.startsWith('H50')?0.024:0.19;
        if (material.name.startsWith('H50')) {
          material.envMapIntensity=0.22;
          material.color.setHex(0x0c1118);
        }
        material.transparent=true; material.depthWrite=false;
      }
    }
  });
  setCamera = ({position,target}) => {
    camera.position.fromArray(position);
    camera.lookAt(...target);
    camera.userData.targetDistance=camera.position.distanceTo(new THREE.Vector3(...target));
    if (activePreset==='interview') fitMossCamera(camera,status.width,status.height,activePreset);
    viewChanged = true;
  };
  function positionOf(object) {return object.getWorldPosition(new THREE.Vector3());}
  let activePreset='';
  let wakeFrame = () => {
    if (contextLost || !status.visible) return;
    clearTimeout(frameTimer);
    frameTimer = setTimeout(frame, 0);
  };
  wakeLayout = wakeFrame;
  let travel = 0;
  let manualTravel=null;
  let startup, startupState='pending', startupAt=0, releaseAt=0;
  let appliedPose=state;
  function setStartupState(value) {
    startupState=value;
    postMessage({type:'startup-state',value});
  }
  beginStartup=()=>{
    if (startupState!=='pending' || status.reduced || !status.visible || phase!=='standby' || manualOrbit || activePreset!=='interview') return;
    startup=createStartupSweep();
    startupAt=performance.now();
    setStartupState('running');
    wakeFrame?.();
  };
  stopStartup=()=>{
    if (startupState==='running') {
      releaseAt=performance.now();
      setStartupState('interrupted');
      wakeFrame?.();
    }
  };
  const eyePosition=()=>frontWindow.localToWorld(frontWindow.geometry.boundingSphere.center.clone());
  function opticsProjection() {
    const p=eyePosition().project(camera);
    return {x:(p.x+1)/2,y:(1-p.y)/2,visible:Math.abs(p.x)<=1 && Math.abs(p.y)<=1 && Math.abs(p.z)<=1,
      yaw:appliedPose.yaw+THREE.MathUtils.radToDeg(shoulderTurn),pitch:appliedPose.pitch,preset:activePreset};
  }
  const overlayTravel=createOverlayTravel(THREE,model,camera,eyePosition);
  const opticAxis=()=>new THREE.Vector3(0,0,1).transformDirection(rig.optical.matrixWorld);
  const neutralAxis=opticAxis();
  const azimuth=v=>Math.atan2(v.x,v.z)*180/Math.PI;
  const elevation=v=>Math.atan2(v.y,Math.hypot(v.x,v.z))*180/Math.PI;
  const neutralAz=azimuth(neutralAxis), neutralEl=elevation(neutralAxis);
  const listenerDirection=new THREE.Vector3();
  function mountTopAtFullZoom() {
    const lens=camera.clone();
    lens.zoom=1;
    lens.clearViewOffset();
    lens.updateMatrixWorld();
    const screen=new THREE.Matrix4().multiplyMatrices(lens.projectionMatrix,lens.matrixWorldInverse);
    const local=new THREE.Matrix4(), vertex=new THREE.Vector3();
    let top=-Infinity;
    const housing=model.meshes[0];
    local.multiplyMatrices(screen,housing.matrixWorld);
    const points=housing.geometry.attributes.position;
    let index=0;
    for (let i=0;i<points.count;i++) {
      const y=vertex.fromBufferAttribute(points,i).applyMatrix4(local).y;
      if (y>top) {top=y;index=i;}
    }
    camera.userData.mountAnchor=vertex.fromBufferAttribute(points,index).applyMatrix4(housing.matrixWorld).toArray();
    return (1-top)/2;
  }
  moveJoints = (radians,pitch) => {
    if (!Number.isFinite(radians) || !Number.isFinite(pitch)) return;
    manualOrbit=true;
    shoulderTurn=radians;
    manualPitch=pitch;
    viewChanged=true;
    wakeFrame?.();
  };
  slideRail = x => {
    manualTravel=rail.slideTo(camera,x);
    viewChanged=true;
    wakeFrame?.();
  };
  setView = preset => {
    idleGaze.reset();
    manualOrbit=false;
    shoulderTurn=0;
    manualPitch=0;
    activePreset=preset;
    fitMossCamera(camera,status.width,status.height,preset);
    const interview = preset==='interview';
    const pose = interview || preset==='whole' ? interviewPoses.standby : {yaw:0,arm:0,pitch:0,travel:0};
    state.yaw=pose.yaw; state.arm=pose.arm; state.pitch=pose.pitch;
    travel=pose.travel;
    manualTravel=null;
    rail.setTravel(travel);
    model.update(0,0,state);
    rail.root.updateMatrixWorld(true);
    const eye = eyePosition();
    const screen = positionOf(rig.screen);
    const bottom = positionOf(rig.bottomLED);
    let target, distance, direction;
    if (interview) {
      const bounds=model.bounds;
      const height=bounds.max.y-bounds.min.y;
      target=bounds.getCenter(new THREE.Vector3());
      target.y-=height*.035;
      distance=height*2.27;
      direction=[0,.18,1];
    } else if (preset === 'optics') {
      target=eye; distance=2.85;
      direction=new THREE.Vector3(0.16,0.085,1).transformDirection(rig.optical.matrixWorld).toArray();
    } else if (preset === 'instruments') {
      target = screen.lerp(bottom, 0.5); distance = 5.7;
      direction = [0.13, -0.14, 1];
    } else if (preset === 'right') {
      const first=positionOf(rig.auxLEDs[0].emitter);
      const last=positionOf(rig.auxLEDs.at(-1).emitter);
      target=first.lerp(last,0.5);
      distance=1.75;
      direction=new THREE.Vector3(-0.13,0.08,1).transformDirection(rig.scanBar.matrixWorld).toArray();
    } else if (preset === 'whole') {
      const bounds = model.bounds;
      target = bounds.getCenter(new THREE.Vector3());
      distance = bounds.getBoundingSphere(new THREE.Sphere()).radius * 4.3;
      direction = [-0.55, 0.18, 1];
    } else {
      target=eye.lerp(screen,.28);
      target.x+=.54;
      distance=11.9; direction=[0,.20,1];
    }
    const position = target.clone().addScaledVector(new THREE.Vector3(...direction).normalize(),distance);
    const framing = {position:position.toArray(),target:target.toArray()};
    setCamera(framing);
    if (interview) {
      camera.userData.mountTop=mountTopAtFullZoom();
      fitMossCamera(camera,status.width,status.height,preset);
    }
    rail.setTravel(travel + overlayTravel.offset);
    layoutDirty=true;
    postMessage({type:'pose',preset,mountTop:camera.userData.mountTop,mountAnchor:camera.userData.mountAnchor,...framing});
  };
  resize = () => {
    layoutDirty=true;
    const {width, height} = status;
    if (!width || !height) return;
    const desiredRatio = software ? Math.min(status.dpr, 0.8) : status.dpr;
    const ratio = Math.min(desiredRatio, Math.sqrt(pixelBudget / (width * height)),
      maxBufferSide / width, maxBufferSide / height);
    const anchor=manualTravel===null ? null : rail.grip(camera,true)?.anchor;
    renderer.setDrawingBufferSize(width,height,ratio);
    fitMossCamera(camera,width,height,activePreset);
    if (Number.isFinite(anchor)) manualTravel=rail.slideTo(camera,anchor);
    viewChanged = true;
    wakeFrame?.();
  };
  resize();
  setView('interview');

  // 保留 CAD 的屏幕平面和玻璃，将实时后台日志渲染为真实纯黑底 128×80 物理自发光 OLED 点阵屏：
  // 灭点保持纯黑 OLED 玻璃底与微弱未点亮像素矩阵结构，仅在亮像素周边产生玻璃内光学散射微光，
  // 金属凹框内缘捕光克制自然，绝不对边框螺丝铺设发灰背光或白雾贴片。
  const originalScreen = rig.effects.screenTexture;
  const COLS = 128, ROWS = 80, PITCH = 5;
  const liveDisplay = new OffscreenCanvas(COLS * PITCH, ROWS * PITCH);
  const live = liveDisplay.getContext('2d');
  const liveTexture = new THREE.CanvasTexture(liveDisplay);
  liveTexture.colorSpace = THREE.SRGBColorSpace;
  liveTexture.anisotropy = 4;
  const pixels = rig.screen.children.find(child => child.material?.emissiveMap === originalScreen);
  // 真实 OLED 圆偏振防眩光层：吸收棚灯漫反射，确保灭点保持深邃纯黑，仅亮像素与玻璃内微光自发光
  pixels.material.color.setHex(0x000000);
  pixels.material.roughness = 1.0;
  pixels.material.metalness = 0.0;
  pixels.material.envMapIntensity = 0.0;
  pixels.material.emissiveMap = liveTexture;
  pixels.material.emissiveIntensity = 2.45;
  pixels.material.needsUpdate = true;

  const OLED_PRESETS = {
    'mono-white': {
      glass:'#030508', well:'rgba(185,212,240,0.040)',
      header:[246,251,255], fg:[235,245,255], dim:[122,150,178], err:[255,112,96],
      core:'rgba(255,255,255,0.52)', lightHex:0xd4e8ff, rimHex:0xbcdbff,
    },
    'ssd-dual': {
      glass:'#020508', well:'rgba(110,205,235,0.038)',
      header:[255,214,82], fg:[68,232,255], dim:[44,148,176], err:[255,124,92],
      core:'rgba(255,255,240,0.48)', lightHex:0x52dcff, rimHex:0x42d2ff,
    },
    'ice-cyan': {
      glass:'#02060a', well:'rgba(96,216,245,0.040)',
      header:[196,252,255], fg:[82,240,255], dim:[48,154,182], err:[255,118,98],
      core:'rgba(235,255,255,0.50)', lightHex:0x5ce8ff, rimHex:0x44dfff,
    },
    amber: {
      glass:'#070401', well:'rgba(245,176,72,0.042)',
      header:[255,232,148], fg:[255,192,74], dim:[168,112,34], err:[255,104,72],
      core:'rgba(255,248,220,0.48)', lightHex:0xffb83c, rimHex:0xffa824,
    },
    'phosphor-green': {
      glass:'#020704', well:'rgba(84,235,142,0.040)',
      header:[204,255,222], fg:[86,255,154], dim:[42,152,88], err:[255,126,96],
      core:'rgba(235,255,242,0.48)', lightHex:0x4eff96, rimHex:0x36ee80,
    },
    'moss-red': {
      glass:'#080202', well:'rgba(245,92,78,0.042)',
      header:[255,196,184], fg:[255,96,82], dim:[164,54,46], err:[255,212,110],
      core:'rgba(255,236,230,0.46)', lightHex:0xff4c3b, rimHex:0xff3d2c,
    },
  };
  let oledPalette = OLED_PRESETS[currentOledPreset] || OLED_PRESETS['mono-white'];

  // 128×80 硬件逻辑像素缓冲（仅记录亮像素，用于驱动物理玻璃内散射光晕 + 离散点阵渲染）
  const emitLow = new OffscreenCanvas(COLS, ROWS);
  const emitCtx = emitLow.getContext('2d', { willReadFrequently: true });
  const cjkScratch = new OffscreenCanvas(12, 12);
  const cjkCtx = cjkScratch.getContext('2d', { willReadFrequently: true });
  const cjkCache = new Map();

  // 经典工业 OLED / VFD 5×7 点阵字库（每字 7 行 5-bit 像素，绝对锐利零发虚）
  const FONT5X7 = {
    ' ': [0,0,0,0,0,0,0],
    '0': [0x0e,0x11,0x13,0x15,0x19,0x11,0x0e], '1': [0x04,0x0c,0x04,0x04,0x04,0x04,0x0e],
    '2': [0x0e,0x11,0x01,0x02,0x04,0x08,0x1f], '3': [0x1f,0x02,0x04,0x02,0x01,0x11,0x0e],
    '4': [0x02,0x06,0x0a,0x12,0x1f,0x02,0x02], '5': [0x1f,0x10,0x1e,0x01,0x01,0x11,0x0e],
    '6': [0x06,0x08,0x10,0x1e,0x11,0x11,0x0e], '7': [0x1f,0x01,0x02,0x04,0x08,0x08,0x08],
    '8': [0x0e,0x11,0x11,0x0e,0x11,0x11,0x0e], '9': [0x0e,0x11,0x11,0x0f,0x01,0x02,0x0c],
    'A': [0x0e,0x11,0x11,0x1f,0x11,0x11,0x11], 'B': [0x1e,0x11,0x11,0x1e,0x11,0x11,0x1e],
    'C': [0x0e,0x11,0x10,0x10,0x10,0x11,0x0e], 'D': [0x1c,0x12,0x11,0x11,0x11,0x12,0x1c],
    'E': [0x1f,0x10,0x10,0x1e,0x10,0x10,0x1f], 'F': [0x1f,0x10,0x10,0x1e,0x10,0x10,0x10],
    'G': [0x0e,0x11,0x10,0x17,0x11,0x11,0x0f], 'H': [0x11,0x11,0x11,0x1f,0x11,0x11,0x11],
    'I': [0x0e,0x04,0x04,0x04,0x04,0x04,0x0e], 'J': [0x07,0x02,0x02,0x02,0x02,0x12,0x0c],
    'K': [0x11,0x12,0x14,0x18,0x14,0x12,0x11], 'L': [0x10,0x10,0x10,0x10,0x10,0x10,0x1f],
    'M': [0x11,0x1b,0x15,0x15,0x11,0x11,0x11], 'N': [0x11,0x19,0x15,0x13,0x11,0x11,0x11],
    'O': [0x0e,0x11,0x11,0x11,0x11,0x11,0x0e], 'P': [0x1e,0x11,0x11,0x1e,0x10,0x10,0x10],
    'Q': [0x0e,0x11,0x11,0x11,0x15,0x12,0x0d], 'R': [0x1e,0x11,0x11,0x1e,0x14,0x12,0x11],
    'S': [0x0f,0x10,0x10,0x0e,0x01,0x01,0x1e], 'T': [0x1f,0x04,0x04,0x04,0x04,0x04,0x04],
    'U': [0x11,0x11,0x11,0x11,0x11,0x11,0x0e], 'V': [0x11,0x11,0x11,0x11,0x11,0x0a,0x04],
    'W': [0x11,0x11,0x11,0x15,0x15,0x1b,0x11], 'X': [0x11,0x11,0x0a,0x04,0x0a,0x11,0x11],
    'Y': [0x11,0x11,0x0a,0x04,0x04,0x04,0x04], 'Z': [0x1f,0x01,0x02,0x04,0x08,0x10,0x1f],
    ':': [0x00,0x04,0x04,0x00,0x04,0x04,0x00], '.': [0x00,0x00,0x00,0x00,0x00,0x06,0x06],
    '-': [0x00,0x00,0x00,0x1f,0x00,0x00,0x00], '/': [0x01,0x01,0x02,0x04,0x08,0x10,0x10],
    '%': [0x18,0x19,0x02,0x04,0x08,0x13,0x03], '#': [0x0a,0x0a,0x1f,0x0a,0x1f,0x0a,0x0a],
    '|': [0x04,0x04,0x04,0x04,0x04,0x04,0x04], '·': [0x00,0x00,0x00,0x06,0x06,0x00,0x00],
    '+': [0x00,0x04,0x04,0x1f,0x04,0x04,0x00], '=': [0x00,0x00,0x1f,0x00,0x1f,0x00,0x00],
    '!': [0x04,0x04,0x04,0x04,0x04,0x00,0x04], '?': [0x0e,0x11,0x02,0x04,0x04,0x00,0x04],
    '>': [0x10,0x08,0x04,0x02,0x04,0x08,0x10], '<': [0x02,0x04,0x08,0x10,0x08,0x04,0x02],
    '_': [0x00,0x00,0x00,0x00,0x00,0x00,0x1f], '(': [0x02,0x04,0x08,0x08,0x08,0x04,0x02],
    ')': [0x08,0x04,0x02,0x02,0x02,0x04,0x08], '[': [0x0e,0x08,0x08,0x08,0x08,0x08,0x0e],
    ']': [0x0e,0x02,0x02,0x02,0x02,0x02,0x0e], '°': [0x06,0x09,0x06,0x00,0x00,0x00,0x00],
    ',': [0x00,0x00,0x00,0x00,0x04,0x04,0x08], '*': [0x00,0x15,0x0e,0x1f,0x0e,0x15,0x00],
  };

  function getCjkBitmap(ch) {
    let bm = cjkCache.get(ch);
    if (bm) return bm;
    cjkCtx.clearRect(0, 0, 12, 12);
    cjkCtx.fillStyle = '#ffffff';
    cjkCtx.font = '700 10px "Microsoft YaHei UI", sans-serif';
    cjkCtx.textBaseline = 'top';
    cjkCtx.fillText(ch, 0, 1);
    const data = cjkCtx.getImageData(0, 0, 10, 9).data;
    bm = [];
    for (let ry = 0; ry < 9; ry++) {
      let bits = 0;
      for (let rx = 0; rx < 10; rx++) {
        if (data[(ry * 10 + rx) * 4 + 3] >= 104) bits |= (1 << (9 - rx));
      }
      bm.push(bits);
    }
    if (cjkCache.size > 256) cjkCache.clear();
    cjkCache.set(ch, bm);
    return bm;
  }

  function dotTextWidth(str) {
    let w = 0;
    for (const ch of str) {
      const up = ch.toUpperCase();
      w += (FONT5X7[up] || ch.charCodeAt(0) < 128) ? 6 : 11;
    }
    return w;
  }

  function drawDotText(str, startX, startY, rgb, maxX = COLS - 2) {
    emitCtx.fillStyle = `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
    let x = Math.round(startX);
    const y = Math.round(startY);
    for (const ch of str) {
      const up = ch.toUpperCase();
      if (FONT5X7[up]) {
        if (x + 5 > maxX) break;
        const rows = FONT5X7[up];
        for (let ry = 0; ry < 7; ry++) {
          const bits = rows[ry];
          if (!bits) continue;
          for (let rx = 0; rx < 5; rx++) {
            if (bits & (1 << (4 - rx))) emitCtx.fillRect(x + rx, y + ry, 1, 1);
          }
        }
        x += 6;
      } else if (ch.charCodeAt(0) < 128) {
        x += 6;
      } else {
        if (x + 10 > maxX) break;
        const bm = getCjkBitmap(ch);
        for (let ry = 0; ry < 9; ry++) {
          const bits = bm[ry];
          if (!bits) continue;
          for (let rx = 0; rx < 10; rx++) {
            if (bits & (1 << (9 - rx))) emitCtx.fillRect(x + rx, y - 1 + ry, 1, 1);
          }
        }
        x += 11;
      }
    }
    return x;
  }

  // 预烘焙纯黑 OLED 暗底玻璃 + 128×80 微弱未点亮像素阵格 + 四边金属凹槽暗角
  const dormantPlate = new OffscreenCanvas(liveDisplay.width, liveDisplay.height);
  const dormantCtx = dormantPlate.getContext('2d');
  function rebuildDormantPlate() {
    const w = liveDisplay.width, h = liveDisplay.height, pal = oledPalette;
    dormantCtx.fillStyle = pal.glass;
    dormantCtx.fillRect(0, 0, w, h);
    dormantCtx.fillStyle = pal.well;
    for (let gy = 0; gy < ROWS; gy++) {
      const py = gy * PITCH + 1;
      for (let gx = 0; gx < COLS; gx++) {
        dormantCtx.fillRect(gx * PITCH + 1, py, 3, 3);
      }
    }
    const aoTop = dormantCtx.createLinearGradient(0, 0, 0, 14);
    aoTop.addColorStop(0, 'rgba(0,0,0,0.68)'); aoTop.addColorStop(1, 'rgba(0,0,0,0)');
    dormantCtx.fillStyle = aoTop; dormantCtx.fillRect(0, 0, w, 14);
    const aoBot = dormantCtx.createLinearGradient(0, h - 10, 0, h);
    aoBot.addColorStop(0, 'rgba(0,0,0,0)'); aoBot.addColorStop(1, 'rgba(0,0,0,0.52)');
    dormantCtx.fillStyle = aoBot; dormantCtx.fillRect(0, h - 10, w, 10);
    const aoL = dormantCtx.createLinearGradient(0, 0, 10, 0);
    aoL.addColorStop(0, 'rgba(0,0,0,0.50)'); aoL.addColorStop(1, 'rgba(0,0,0,0)');
    dormantCtx.fillStyle = aoL; dormantCtx.fillRect(0, 0, 10, h);
    const aoR = dormantCtx.createLinearGradient(w - 10, 0, w, 0);
    aoR.addColorStop(0, 'rgba(0,0,0,0)'); aoR.addColorStop(1, 'rgba(0,0,0,0.50)');
    dormantCtx.fillStyle = aoR; dormantCtx.fillRect(w - 10, 0, 10, h);
  }
  rebuildDormantPlate();

  // 1) 克制真实的物理矩形面光源（RectAreaLight，给凹框内壁金属沿与下方护栏一丝真实色光倒影，不漂白外壳）
  const areaW = 10.6 * model.rawScale;
  const areaH = 6.6 * model.rawScale;
  const screenAreaLight = new THREE.RectAreaLight(oledPalette.lightHex, 6.2, areaW, areaH);
  screenAreaLight.position.set(0, 0, 0.10);
  screenAreaLight.rotation.y = Math.PI;
  rig.screen.add(screenAreaLight);

  // 2) 紧贴金属凹槽内沿的极窄光学捕光环（仅外扩 0.48mm，远在 4 颗螺丝之前衰减为 0，绝无大面积白雾）
  const rimW = 160, rimH = 112;
  const rimPlaneW = 12.8, rimPlaneH = 8.8;
  const rimCanvas = new OffscreenCanvas(rimW, rimH);
  const rimCtx = rimCanvas.getContext('2d');
  const rimImg = rimCtx.createImageData(rimW, rimH);
  const rd = rimImg.data;
  const cornerR = 0.45;
  const halfBx = 5.44 - cornerR, halfBy = 3.43 - cornerR;
  for (let py = 0; py < rimH; py++) {
    const wy = ((py + 0.5) / rimH - 0.5) * rimPlaneH;
    for (let px = 0; px < rimW; px++) {
      const wx = ((px + 0.5) / rimW - 0.5) * rimPlaneW;
      const qx = Math.abs(wx) - halfBx;
      const qy = Math.abs(wy) - halfBy;
      const d = Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0) - cornerR;
      let v = 0;
      if (d > 0 && d < 0.80) {
        const fall = Math.exp(-d / 0.22);
        const edge = 1 - Math.min(1, d / 0.80);
        v = 0.48 * fall * edge * edge;
      }
      const idx = (py * rimW + px) * 4;
      rd[idx] = 255; rd[idx + 1] = 255; rd[idx + 2] = 255;
      rd[idx + 3] = Math.min(255, Math.round(v * 255));
    }
  }
  rimCtx.putImageData(rimImg, 0, 0);
  const rimTexture = new THREE.CanvasTexture(rimCanvas);
  rimTexture.colorSpace = THREE.SRGBColorSpace;
  const screenHalo = new THREE.Mesh(
    new THREE.PlaneGeometry(rimPlaneW, rimPlaneH),
    new THREE.MeshBasicMaterial({
      map: rimTexture,
      color: oledPalette.rimHex,
      transparent: true,
      opacity: 0.28,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      toneMapped: false,
    }),
  );
  screenHalo.position.set(0, 0, 0.806);
  screenHalo.renderOrder = 2;
  rig.screen.add(screenHalo);

  const lines = [], visibleLines = 6;
  let scroll = 0, dirty = true, screenPulse = 0, rxSeq = 0, lastBlinkPhase = -1;

  setOledPreset = next => {
    const valid = OLED_PRESETS[next] ? next : 'mono-white';
    currentOledPreset = valid;
    oledPalette = OLED_PRESETS[valid];
    rebuildDormantPlate();
    screenAreaLight.color.setHex(oledPalette.lightHex);
    screenHalo.material.color.setHex(oledPalette.rimHex);
    dirty = true;
    wakeFrame?.();
  };

  function compactLogText(raw) {
    return String(raw || '')
      .replace(/^(?:BUS[-·\s]+THERM|BUS[-·\s]+MEMIO|CORE[-·\s]+550W|MATX[-·\s]+BRAIN)\s*\|\s*/i, '')
      .replace(/\s*·\s*/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function pushWrappedDotRows(stamp, text, isErr) {
    const maxDots = COLS - 6;
    const prefix = stamp ? stamp + ' ' : '';
    const prefixW = dotTextWidth(prefix);
    const words = text.split(' ');
    let cur = '';
    let first = true;
    const flush = () => {
      if (!cur && !first) return;
      lines.push({ stamp: first ? stamp : '', text: cur, error: isErr });
      cur = '';
      first = false;
    };
    for (const word of words) {
      if (!word) continue;
      const limit = first ? maxDots - prefixW : maxDots - 6;
      const candidate = cur ? cur + ' ' + word : word;
      if (dotTextWidth(candidate) <= limit) {
        cur = candidate;
      } else {
        if (cur) flush();
        const lim2 = first ? maxDots - prefixW : maxDots - 6;
        if (dotTextWidth(word) <= lim2) {
          cur = word;
        } else {
          let part = '';
          for (const ch of word) {
            const lim3 = first ? maxDots - prefixW : maxDots - 6;
            if (part && dotTextWidth(part + ch) > lim3) {
              cur = part;
              flush();
              part = ch;
            } else {
              part += ch;
            }
          }
          cur = part;
        }
      }
    }
    if (cur || first) flush();
  }

  appendRuntimeLog = entry => {
    const time = new Date(typeof entry.time === 'number' && entry.time < 1e12 ? entry.time * 1000 : entry.time);
    const stamp = Number.isFinite(time.getTime()) ? time.toTimeString().slice(0, 5) : '';
    const isErr = Boolean(entry.error || entry.level === 'crit');
    const cleaned = compactLogText(entry.text);
    if (cleaned) pushWrappedDotRows(stamp, cleaned, isErr);
    if (lines.length > 160) {
      const removed = lines.length - 160;
      lines.splice(0, removed);
      scroll = Math.max(0, scroll - removed);
    }
    rxSeq = (rxSeq + 1) % 1000;
    screenPulse = 1;
    dirty = true;
  };
  logs.forEach(appendRuntimeLog);

  function updateDisplay(dt) {
    const target = Math.max(0, lines.length - visibleLines);
    const blinkPhase = status.reduced ? 0 : Math.floor(elapsed * 1.8) % 2;
    const prevPulse = screenPulse;
    screenPulse = status.reduced ? 0 : Math.max(0, screenPulse - dt * 2.8);
    pixels.material.emissiveIntensity = 2.45 + 0.28 * screenPulse;
    screenAreaLight.intensity = 6.2 + 2.0 * screenPulse;
    screenHalo.material.opacity = 0.28 + 0.08 * screenPulse;

    if (!dirty && scroll === target && prevPulse <= 0.005 && blinkPhase === lastBlinkPhase) return;
    lastBlinkPhase = blinkPhase;
    const backlog = target - scroll;
    scroll = status.reduced ? target : Math.min(target, scroll + (11 + 4 * backlog) * dt);

    const w = liveDisplay.width, h = liveDisplay.height, pal = oledPalette;

    // 1. 在 128×80 逻辑像素缓冲上只点亮实际发出的 OLED 像素点
    emitCtx.clearRect(0, 0, COLS, ROWS);
    // 顶部状态区（在 SSD1306 黄蓝双色模式下为经典琥珀黄区）
    drawDotText('550W·H50', 3, 3, pal.header, 70);
    const rxStr = `RX#${String(rxSeq).padStart(3, '0')}`;
    const rxW = dotTextWidth(rxStr);
    drawDotText(rxStr, COLS - 9 - rxW, 3, pal.header, COLS - 8);
    if (blinkPhase === 0) {
      emitCtx.fillStyle = `rgb(${pal.header[0]},${pal.header[1]},${pal.header[2]})`;
      emitCtx.fillRect(COLS - 6, 4, 3, 5);
    }
    // 1-dot 硬件分隔线
    emitCtx.fillStyle = `rgb(${pal.header[0]},${pal.header[1]},${pal.header[2]})`;
    for (let gx = 2; gx < COLS - 2; gx++) {
      if (gx % 2 === 0 || gx < 56 || gx > COLS - 48) emitCtx.fillRect(gx, 12, 1, 1);
    }

    // 主体 6 行实时点阵遥测日志（每行 10 像素间距，整齐对齐像素网格）
    const startIdx = Math.floor(scroll);
    const endIdx = Math.min(lines.length, startIdx + visibleLines);
    if (!lines.length) {
      drawDotText('550W LINK READY', 3, 17, pal.dim);
      drawDotText('AWAITING TELEMETRY', 3, 27, pal.fg);
    }
    for (let i = startIdx; i < endIdx; i++) {
      const row = lines[i];
      const gy = 16 + (i - startIdx) * 10;
      if (gy > ROWS - 9) break;
      let gx = 3;
      if (row.stamp) {
        gx = drawDotText(row.stamp, gx, gy, pal.dim, COLS - 3) + 2;
      } else {
        gx = 7;
      }
      drawDotText(row.text, gx, gy, row.error ? pal.err : pal.fg, COLS - 3);
    }
    if (blinkPhase === 0) {
      emitCtx.fillStyle = `rgb(${pal.fg[0]},${pal.fg[1]},${pal.fg[2]})`;
      emitCtx.fillRect(COLS - 7, ROWS - 8, 4, 6);
    }

    // 2. 合成到 640×400 显示纹理：纯黑未点亮像素阵底板
    live.clearRect(0, 0, w, h);
    live.drawImage(dormantPlate, 0, 0);

    // 3. 仅围绕亮像素产生真实的玻璃内光学散射微光（双尺度 PSF 散射，暗区保持深邃纯黑）
    live.save();
    live.globalCompositeOperation = 'lighter';
    live.imageSmoothingEnabled = true;
    if ('filter' in live) {
      live.filter = 'blur(10px)';
      live.globalAlpha = 0.38 + 0.10 * screenPulse;
      live.drawImage(emitLow, 0, 0, w, h);
      live.filter = 'blur(3px)';
      live.globalAlpha = 0.68 + 0.16 * screenPulse;
      live.drawImage(emitLow, 0, 0, w, h);
      live.filter = 'none';
    } else {
      live.globalAlpha = 0.42;
      live.drawImage(emitLow, 0, 0, w, h);
    }
    live.restore();

    // 4. 在微光上方叠加逐点锐利的 128×80 独立发光微像素（3.4×3.4 亮珠 + 1.6×1.6 中心灯芯 + 1.6px 暗槽）
    const pix = emitCtx.getImageData(0, 0, COLS, ROWS).data;
    for (let gy = 0; gy < ROWS; gy++) {
      const py = gy * PITCH + 0.8;
      for (let gx = 0; gx < COLS; gx++) {
        const idx = (gy * COLS + gx) * 4;
        const a = pix[idx + 3];
        if (!a) continue;
        const r = pix[idx], g = pix[idx + 1], b = pix[idx + 2];
        const px = gx * PITCH + 0.8;
        live.fillStyle = `rgb(${r},${g},${b})`;
        live.fillRect(px, py, 3.4, 3.4);
        if (r + g + b > 380) {
          live.fillStyle = pal.core;
          live.fillRect(px + 0.9, py + 0.9, 1.6, 1.6);
        }
      }
    }

    // 5. 极轻微的表层防护玻璃斜向微反光（增强真实物理玻璃覆盖层次）
    const sheen = live.createLinearGradient(0, 0, w * 0.48, h * 0.62);
    sheen.addColorStop(0, 'rgba(255,255,255,0.036)');
    sheen.addColorStop(0.42, 'rgba(255,255,255,0.010)');
    sheen.addColorStop(0.50, 'rgba(255,255,255,0)');
    live.fillStyle = sheen;
    live.fillRect(0, 0, w, h);

    liveTexture.needsUpdate = true;
    dirty = false;
  }
  const smooth = THREE.MathUtils.damp;
  let last = 0, elapsed = 0, envelope = 0, ready = false;
  let responseMix=0, runningPosition=0;
  const runningEpoch=performance.now();
  inspectScene=()=>({ready,activePreset,source,software,modelRelease,meshCount:stats.meshes,triangles:stats.triangles,
    camera:camera.position.toArray(),cameraRange:camera.userData.targetDistance,viewport:[status.width,status.height,canvas.width,canvas.height,camera.aspect],
    level:envelope,yaw:appliedPose.yaw,pitch:appliedPose.pitch,arm:appliedPose.arm,travel,phase,manualOrbit,startupState,
    idleGaze:idleGaze.inspect(performance.now()/1000),
    shoulderTurn,manualPitch,mountTop:camera.userData.mountTop,
    mountEdgeY:(1-new THREE.Vector3().fromArray(camera.userData.mountAnchor).project(camera).y)*status.height/2,
    railOffset:overlayTravel.offset,railTarget:overlayTravel.target,railPosition:rail.root.position.x,
    railGrip:rail.grip(camera),
    gaze:{eye:eyePosition().toArray(),forward:opticAxis().toArray()},
    eyePixel:(()=>{const p=opticsProjection();return [p.x*status.width,p.y*status.height];})(),
    responseMix,runningPosition,
    emitters:rig.auxLEDs.map(led=>led.emitter.material.emissiveIntensity),
    ledPixels:rig.auxLEDs.map(({emitter})=>{
      const p=positionOf(emitter).project(camera);
      return [(p.x+1)*status.width/2,(1-p.y)*status.height/2];
    }),
    tof:emitter.material.emissiveIntensity,bottom:rig.bottomLED.material.emissiveIntensity,
    dualTof:{outer:outer.rotation.z,inner:inner.rotation.z,fixed:fixed.rotation.z,meshCounts:tofMotion.meshes,
      frame:sourceFrame,source:tofMotion.sha256},
    decalAudit:auditPanelDecals(THREE,scene),
    display:liveTexture.version,oledPreset:currentOledPreset,logs:logs.map(entry=>entry.text),frames:renderer.info.render.frame});
  let sourceFrame=tofMotion.frames[0], previousOptics;
  function frame() {
    const now = performance.now();
    if (contextLost || !status.visible) {last=now; frameTimer=0; return;}
    const dt = Math.min((now - last) / 1000, 0.7);
    last = now; elapsed += dt;
    envelope = smooth(envelope, level, 11, dt);
    responseMix=smooth(responseMix,audioActivity,audioActivity>responseMix?16:5,dt);
    runningPosition=(now-runningEpoch)*0.0017%6;
    for (let i = 0; i < bands.length; i++)
      bands[i] = smooth(bands[i], inputs[i], inputs[i] > bands[i] ? 19 : 8, dt);
    const listen = phase === 'listen', talk = source === 'voice';
    const portrait=activePreset==='interview' || activePreset==='front';
    const overview=portrait || activePreset==='whole';
    const autonomous=overview && !manualOrbit && !status.reduced && !status.opening;
    const glance=idleGaze.sample(now/1000,ready && autonomous && !startup && phase==='standby');
    if (overview) {
      const next=interviewPoses[autonomous ? phase : 'standby'] || interviewPoses.standby;
      let yaw=glance?.yaw ?? next.yaw, arm=glance?.arm ?? next.arm, pitch=glance?.pitch ?? next.pitch;
      if (portrait && next!==interviewPoses.standby) {
        // 肩轴和肘轴合计决定方位。沿真实镜头到观察者的方向注视，滑轨避让后同样适用。
        listenerDirection.subVectors(camera.position,eyePosition());
        arm=next.arm*.3;
        const aside=phase==='think' ? -1.6 : 0;
        yaw=THREE.MathUtils.clamp(azimuth(listenerDirection)-neutralAz+aside,-20,20)-arm;
        pitch=THREE.MathUtils.clamp(neutralEl-elevation(listenerDirection),-16,16)+(phase==='think' ? 1 : 0);
      }
      const response=glance ? 9 : listen ? 6.3 : 3.2;
      travel=smooth(travel,next.travel,1.45,dt);
      state.yaw=smooth(state.yaw,yaw,response,dt);
      state.arm=smooth(state.arm,arm,response,dt);
      state.pitch=smooth(state.pitch,pitch,response,dt);
    } else {
      state.yaw = smooth(state.yaw, listen ? -6 : talk ? 2.8 - 2*envelope : 0, 3, dt);
      state.arm = smooth(state.arm, listen ? 3 : talk ? -1.8 : 0, 3, dt);
      state.pitch = smooth(state.pitch, listen ? -3 : talk ? 1.5 + 2.4*envelope : 0, 3, dt);
      if (status.reduced) state.yaw = state.pitch = state.arm = 0;
    }
    state.speaking = talk;
    let sweep, weight=1;
    if (startup) {
      const t=(now-startupAt)/(startup.duration*1000);
      if (releaseAt) weight=status.reduced ? 0 : 1-THREE.MathUtils.smoothstep(now-releaseAt,0,140);
      if (t>=1 || weight<=0) {
        startup=null;
        if (startupState==='running') setStartupState('complete');
      } else sweep=startup.sample(t);
    }
    appliedPose=sweep ? {...state,yaw:state.yaw+sweep.yaw*weight,pitch:state.pitch+sweep.pitch*weight,arm:state.arm+sweep.arm*weight} : state;
    if (manualPitch) appliedPose={...appliedPose,pitch:THREE.MathUtils.clamp(appliedPose.pitch+manualPitch,-40,40)};
    model.update(dt, elapsed, appliedPose);
    model.nodes.shoulderYaw.rotateY(shoulderTurn);
    for (const led of rig.auxLEDs) {
      // 安静时亮峰连续通过六颗灯，开口后由各自的频段接管。两种状态间平滑过渡。
      const offset=Math.abs(led.index-runningPosition);
      const distance=Math.min(offset,6-offset);
      const running=status.reduced ? (led.index===0 ? 0.34 : 0) : 0.75*Math.exp(-3.4*distance*distance);
      const sound=(status.reduced ? 0.42 : 1)*bands[led.index]**0.77;
      const chase=sweep ? Math.exp(-2.8*(led.index-sweep.lamp)**2) : running;
      const illumination=sweep ? THREE.MathUtils.lerp(running,chase,sweep.lampMix*weight) : running;
      const power=THREE.MathUtils.lerp(illumination,sound,responseMix);
      const die = led.emitter.material;
      die.emissiveIntensity = 0.56 + 8.8 * power;
      die.color.setScalar(0.16 + 0.75 * power);
      const lens = led.lens.material;
      lens.emissive.setRGB(1,0.026+0.24*power**1.5,0.004+0.025*power*power);
      lens.emissiveIntensity = 0.22 + 5.4 * power;
      lens.opacity = 0.26 + 0.35 * power;
    }
    const cycle=phase==='think' ? 1.75 : 3.2;
    const breath=status.reduced ? .55 : .5-.5*Math.cos(now*2*Math.PI/(1000*cycle));
    const glow=talk || listen ? (status.reduced ? .85 : .23+1.55*envelope)
      : .22+.95*breath;
    const tofPhase=status.reduced ? 0 : (elapsed*tofMotion.fps)%tofMotion.outer.length;
    const previous=Math.floor(tofPhase), next=(previous+1)%tofMotion.outer.length;
    sourceFrame=tofMotion.frames[0]+tofPhase;
    outer.rotation.z=THREE.MathUtils.lerp(tofMotion.outer[previous],tofMotion.outer[next],tofPhase-previous);
    inner.rotation.z=THREE.MathUtils.lerp(tofMotion.inner[previous],tofMotion.inner[next],tofPhase-previous);
    emitter.material.emissiveIntensity=glow;
    rig.bottomLED.material.emissiveIntensity += envelope * 2.5;
    updateDisplay(dt);
    const updateSubject=layoutDirty;
    if (updateSubject) {
      overlayTravel.plan(overlayRects);
      layoutDirty=false;
    }
    const layoutTravel=overlayTravel.advance(dt,status.reduced);
    rail.setTravel(manualTravel ?? travel+layoutTravel);
    renderer.render(scene, camera);
    const optics=opticsProjection();
    if (viewChanged || updateSubject || !previousOptics || optics.visible!==previousOptics.visible ||
        Math.abs(optics.x-previousOptics.x)*status.width>.6 || Math.abs(optics.y-previousOptics.y)*status.height>.6 ||
        Math.abs(optics.yaw-previousOptics.yaw)>.2 || Math.abs(optics.pitch-previousOptics.pitch)>.2) {
      postMessage({type:'optics',optics,grip:rail.grip(camera),subject:updateSubject ? overlayTravel.footprint : undefined});
      previousOptics=optics;
    }
    if (!ready) {ready=true; postMessage({type:'ready',software,stats:{...stats,modelRelease}});}
    const moving=startup || glance;
    const interval = moving ? (software ? 70 : 33) : software ? (viewChanged ? 100 : status.reduced && source==='idle' ? 900 : 260) : (source === 'idle' ? 90 : 45);
    viewChanged = false;
    // 首段语音运算期间给 CUDA 留出绘图间隙，拖动视角仍然即时重绘。
    frameTimer=setTimeout(frame,firstVoice && !manualOrbit ? Math.max(150,interval) : interval);
  }
  if (gl.getExtension('KHR_parallel_shader_compile')) {
    loadStage('shaders');
    await renderer.compileAsync(scene, camera);
  }
  loadStage('frame');
  frame();
}

self.onmessage = ({data}) => {
  if (data.type === 'init') {
    status = data.status;
    setup(data.canvas).catch(error => postMessage({type:'error',message:error.message}));
  } else if (data.type === 'image') {
    const promise=pendingImages.get(data.id);
    if(data.bitmap) promise.resolve(data.bitmap); else promise.reject(Error(data.message));
    pendingImages.delete(data.id);
  } else if (data.type === 'status') {
    const wasVisible = status.visible;
    const changed = data.status.width !== status.width || data.status.height !== status.height || data.status.dpr !== status.dpr;
    status = data.status;
    if (!status.visible || status.reduced || status.opening) idleGaze.reset();
    if(changed) resize?.();
    if(status.theme) setTheme?.(status.theme);
    if (!wasVisible && status.visible && !contextLost) wakeLayout?.();
  } else if (data.type === 'overlays') {
    overlayRects=data.rects; layoutDirty=true; wakeLayout?.();
  } else if (data.type === 'framing') {
    layoutDirty=true; wakeLayout?.();
  } else if (data.type === 'startup') beginStartup?.();
  else if (data.type === 'stop-startup') stopStartup?.();
  else if (data.type === 'view') setView?.(data.preset);
  else if (data.type === 'orbit') manualOrbit=true;
  else if (data.type === 'joints') moveJoints?.(data.radians,data.pitch);
  else if (data.type === 'rail') slideRail?.(data.x);
  else if (data.type === 'camera') setCamera?.(data);
  else if (data.type === 'signal') signal(data.detail);
  else if (data.type === 'oled-preset' && typeof data.preset === 'string') {
    currentOledPreset = data.preset;
    setOledPreset?.(data.preset);
  } else if (data.type==='runtime-log' && typeof data.entry?.text==='string') {
    const entry={
      text:data.entry.text.slice(0,2048),
      time:data.entry.time,
      source:typeof data.entry.source==='string' ? data.entry.source.slice(0,16) : '',
      level:typeof data.entry.level==='string' ? data.entry.level : '',
      error:!!(data.entry.error || data.entry.level==='crit'),
    };
    logs.push(entry);
    if (logs.length>64) logs.shift();
    appendRuntimeLog?.(entry);
  }
};
