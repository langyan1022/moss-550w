import {PerspectiveCamera} from 'moss-three';
import {OrbitControls} from 'moss-orbit';
import {fitMossCamera} from './moss-projection.source.js';
import {observeOverlaySpace} from './overlay-travel.js';

const loadLabel = document.getElementById('model-load-stage');
const loadMeter = document.querySelector('.model-load-meter');
const loadReading = document.querySelector('.model-load-reading');
const loadLabels = {
  graphics:'初始化三维渲染器', lighting:'生成环境光照', geometry:'载入机身与镜组',
  exterior:'读取 550W 机身', optics:'读取 TOF 镜组', 'optics-ready':'镜组已就绪',
  assembly:'装配机身与机械臂', shaders:'准备三维材质', frame:'绘制三维首帧'
};
function failed(canvas, error) {
  if (canvas.dataset.error) return;
  canvas.dataset.error = error.message || 'initialization';
  console.error('550W 三维显示初始化失败', error);
  loadLabel.textContent = /WebGL|OffscreenCanvas|transferControlToOffscreen/i.test(error.message)
    ? '三维渲染未启动，请检查浏览器硬件加速。' : '三维模型载入失败，请刷新重试。';
}

function start(canvas, lifetime) {
  const {signal} = lifetime;
  const onStop = cleanup => signal.addEventListener('abort',cleanup,{once:true});
  const camera = new PerspectiveCamera(29, 1, 0.025, 100);
  const controls = new OrbitControls(camera, canvas);
  onStop(()=>controls.dispose());
  controls.enabled = false;
  controls.enableRotate = false;
  controls.enableDamping = true;
  controls.dampingFactor = 0.18;
  controls.enableZoom = false;
  controls.panSpeed = 0.8;
  canvas.style.touchAction = 'none';
  const renderURL = new URL('./moss-render.js',import.meta.url);
  renderURL.searchParams.set('v',MOSS_RENDER_SHA256);
  const surface = canvas.transferControlToOffscreen();
  const worker = new Worker(renderURL, {type:'module',name:'550W CAD'});
  onStop(()=>{worker.onmessage=null;worker.terminate();});
  function fail(error) {
    if (signal.aborted) return;
    failed(canvas,error);
    lifetime.abort();
  }
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let activePreset = 'interview';
  let visible = true, ready = false;
  const stage=canvas.closest('main');
  const instruments=stage.querySelector('.camera-overlay');
  const yawReadout=instruments.querySelector('[data-angle="yaw"]');
  const pitchReadout=instruments.querySelector('[data-angle="pitch"]');
  const checkIndicator=instruments.querySelector('.check-indicator');
  const degrees=value=>{
    const angle=Math.round(value);
    return (angle>0 ? '+' : '')+angle+'°';
  };
  function displayOptics({x,y,visible,yaw,pitch}) {
    instruments.hidden=false;
    instruments.toggleAttribute('data-on-screen',visible);
    instruments.style.setProperty('--optic-x',(100*x).toFixed(2)+'%');
    instruments.style.setProperty('--optic-y',(100*y).toFixed(2)+'%');
    const az=degrees(((yaw+180)%360+360)%360-180), el=degrees(pitch);
    if (yawReadout.textContent!==az) yawReadout.textContent=az;
    if (pitchReadout.textContent!==el) pitchReadout.textContent=el;
  }
  let startupPending=true;
  function maybeStart() {
    if (!startupPending || !ready || !visible || document.hidden || stage.hasAttribute('data-opening')) return;
    startupPending=false;
    if (reduced.matches) {canvas.dataset.selfTest='reduced'; return;}
    canvas.dataset.selfTest='requested';
    worker.postMessage({type:'startup'});
  }
  function takeControl() {
    if (!startupPending && canvas.dataset.selfTest!=='running' && canvas.dataset.selfTest!=='requested') return;
    startupPending=false;
    canvas.dataset.selfTest='cancelled';
    worker.postMessage({type:'stop-startup'});
  }
  const openingObserver=new MutationObserver(()=>{
    worker.postMessage({type:'status',status:readStatus()});
    if (stage.hasAttribute('data-opening')) {
      if (canvas.dataset.selfTest==='running' || canvas.dataset.selfTest==='requested') takeControl();
    } else maybeStart();
  });
  openingObserver.observe(stage,{attributes:true,attributeFilter:['data-opening']});
  onStop(()=>openingObserver.disconnect());
  let theme = getComputedStyle(document.documentElement).getPropertyValue('--signal').trim();
  function readStatus() {
    const {width,height} = canvas.getBoundingClientRect();
    return {width:Math.max(1,Math.round(width)),height:Math.max(1,Math.round(height)),
            dpr:devicePixelRatio || 1,visible:visible && !document.hidden,reduced:reduced.matches,opening:stage.hasAttribute('data-opening'),theme};
  }
  const status = readStatus();
  fitMossCamera(camera,status.width,status.height,activePreset);
  worker.postMessage({type:'init',canvas:surface,status},[surface]);
  const readOledPreset = () => {
    try { return localStorage.getItem('moss-fui.oled-preset') || 'mono-white'; } catch { return 'mono-white'; }
  };
  worker.postMessage({type:'oled-preset',preset:readOledPreset()});
  window.addEventListener('moss-oled-preset', e => {
    if (typeof e.detail === 'string') worker.postMessage({type:'oled-preset',preset:e.detail});
  }, {signal});
  window.addEventListener('storage', e => {
    if (e.key === 'moss-fui.oled-preset') worker.postMessage({type:'oled-preset',preset:e.newValue || 'mono-white'});
  }, {signal});
  const panelSpace = observeOverlaySpace(canvas, rects => worker.postMessage({type:'overlays',rects}));
  onStop(()=>panelSpace.disconnect());
  const runtimeLog = new EventSource('/api/runtime-log/stream');
  onStop(()=>{runtimeLog.onmessage=null;runtimeLog.close();});
  const runtimeLines = document.getElementById('runtime-lines');
  const logClock = new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
  runtimeLog.onmessage = event => {
    try {
      const entry = JSON.parse(event.data);
      if (typeof entry.text!=='string') return;
      worker.postMessage({type:'runtime-log',entry});
      if (runtimeLines) {
        const row = document.createElement('li');
        const stamp = document.createElement('time');
        stamp.textContent = logClock.format(entry.time);
        const message = document.createElement('span');
        message.textContent = entry.text;
        row.title = entry.text;
        row.append(stamp,message);
        runtimeLines.append(row);
        if (runtimeLines.children.length > 4) runtimeLines.firstElementChild.remove();
      }
    } catch { /* 只有完整的后台日志行才显示在 H50 上。 */ }
  };
  function updateStatus() {
    const status = readStatus();
    fitMossCamera(camera,status.width,status.height,activePreset);
    worker.postMessage({type:'status',status});
    if ((reduced.matches || !status.visible) && (canvas.dataset.selfTest==='running' || canvas.dataset.selfTest==='requested')) takeControl();
    maybeStart();
  }
  const sizeObserver = new ResizeObserver(updateStatus);
  sizeObserver.observe(canvas);
  onStop(()=>sizeObserver.disconnect());
  function watchPixelRatio() {
    const resolution = matchMedia('(resolution: ' + devicePixelRatio + 'dppx)');
    resolution.addEventListener('change',()=>{updateStatus();watchPixelRatio();},{once:true,signal});
  }
  watchPixelRatio();
  const viewportObserver = new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;updateStatus();});
  viewportObserver.observe(canvas);
  onStop(()=>viewportObserver.disconnect());
  document.addEventListener('visibilitychange',updateStatus,{signal});
  reduced.addEventListener('change',updateStatus,{signal});
  window.addEventListener('moss-theme',e=>{theme=e.detail.color;updateStatus();},{signal});
  const pointers=new Map();
  let shoulderTurn=0, wristPitch=0, turnFrame=0, poseFrame=0, framingTimer=0, grip, multiTouch=false;
  let suppressControlsChange=false;
  onStop(()=>{cancelAnimationFrame(turnFrame);cancelAnimationFrame(poseFrame);clearTimeout(framingTimer);});
  function gripAt(event) {
    if (!grip) return null;
    const rect=canvas.getBoundingClientRect();
    const x=(event.clientX-rect.left)/rect.width, y=(event.clientY-rect.top)/rect.height;
    return x>=grip.left && x<=grip.right && y>=grip.top && y<=grip.bottom ? x-grip.anchor : null;
  }
  function updateFramingAfterInput() {
    clearTimeout(framingTimer);
    framingTimer=setTimeout(()=>worker.postMessage({type:'framing'}),140);
  }
  function applyJointDrag(dx, dy) {
    if (!dx && !dy) return;
    const span=Math.max(1, Math.min(canvas.clientWidth,canvas.clientHeight));
    shoulderTurn+=dx*2*Math.PI/span;
    wristPitch=Math.min(18,Math.max(-18,wristPitch+dy*65/span));
    if (!turnFrame) turnFrame=requestAnimationFrame(()=>{
      turnFrame=0;
      worker.postMessage({type:'joints',radians:shoulderTurn,pitch:wristPitch});
      updateFramingAfterInput();
    });
  }
  canvas.addEventListener('pointerdown',event=>{
    takeControl();
    if (!ready || event.button!==0 || event.shiftKey || event.ctrlKey || event.metaKey) return;
    const offset=gripAt(event);
    canvas.style.cursor=offset===null?'grabbing':'ew-resize';
    pointers.set(event.pointerId,{x:event.clientX,y:event.clientY,offset});
    if (pointers.size>1) multiTouch=true;
  },{signal});
  canvas.addEventListener('pointermove',event=>{
    const last=pointers.get(event.pointerId);
    if (last===undefined) {
      if (!pointers.size && !event.buttons) canvas.style.cursor=gripAt(event)===null?'grab':'ew-resize';
      return;
    }
    const dx=event.clientX-last.x, dy=event.clientY-last.y;
    last.x=event.clientX; last.y=event.clientY;
    if (multiTouch || event.shiftKey || event.ctrlKey || event.metaKey) return;
    if (!dx && !dy) return;
    if (last.offset!==null) {
      if (dx) {
        const rect=canvas.getBoundingClientRect();
        worker.postMessage({type:'rail',x:(event.clientX-rect.left)/rect.width-last.offset});
        updateFramingAfterInput();
      }
      return;
    }
    applyJointDrag(dx, dy);
  },{signal});
  for (const type of ['pointerup','pointercancel','lostpointercapture'])
    canvas.addEventListener(type,event=>{
      pointers.delete(event.pointerId);
      if (!pointers.size) {multiTouch=false;canvas.style.cursor='grab';}
    },{signal});
  window.addEventListener('blur',()=>{pointers.clear();multiTouch=false;canvas.style.cursor='grab';},{signal});
  controls.addEventListener('start',()=>{takeControl();canvas.style.cursor='grabbing';worker.postMessage({type:'orbit'});});
  controls.addEventListener('end',()=>{canvas.style.cursor='grab';});
  controls.addEventListener('change',()=>{
    if (suppressControlsChange || poseFrame || !ready || signal.aborted) return;
    poseFrame=requestAnimationFrame(()=>{
      poseFrame=0;
      const {width,height}=readStatus();
      fitMossCamera(camera,width,height,activePreset);
      worker.postMessage({type:'camera',position:camera.position.toArray(),target:controls.target.toArray()});
      updateFramingAfterInput();
      suppressControlsChange=true;
      try { controls.update(); } finally { suppressControlsChange=false; }
    });
  });
  function preset(name) {
    takeControl();
    cancelAnimationFrame(turnFrame);
    turnFrame=0;
    cancelAnimationFrame(poseFrame);
    poseFrame=0;
    shoulderTurn=0;
    wristPitch=0;
    pointers.clear();
    multiTouch=false;
    grip=null;
    worker.postMessage({type:'view',preset:name});
  }
  canvas.addEventListener('dblclick',()=>document.getElementById('reset-view')?.click(),{signal});
  canvas.addEventListener('moss-reset-view',()=>preset('interview'),{signal});
  canvas.addEventListener('moss-view-preset',e=>preset(e.detail),{signal});
  canvas.addEventListener('moss-pinch-zoom',e=>{
    const deltaY=Number(e.detail?.deltaY);
    if (!ready || !Number.isFinite(deltaY) || !deltaY) return;
    takeControl();
    worker.postMessage({type:'orbit'});
    const dx=camera.position.x-controls.target.x;
    const dy=camera.position.y-controls.target.y;
    const dz=camera.position.z-controls.target.z;
    const dist=Math.hypot(dx,dy,dz);
    if (!dist) return;
    const minD=Math.max(0.35,controls.minDistance || 0.35);
    const maxD=Math.min(12,Number.isFinite(controls.maxDistance) ? controls.maxDistance : 12);
    const nextDist=Math.max(minD,Math.min(maxD,dist*Math.exp(deltaY*0.0022)));
    const ratio=nextDist/dist;
    camera.position.set(controls.target.x+dx*ratio,controls.target.y+dy*ratio,controls.target.z+dz*ratio);
    const {width,height}=readStatus();
    fitMossCamera(camera,width,height,activePreset);
    suppressControlsChange=true;
    try { controls.enableDamping=false; controls.update(); } finally { controls.enableDamping=true; suppressControlsChange=false; }
    worker.postMessage({type:'camera',position:camera.position.toArray(),target:controls.target.toArray()});
    updateFramingAfterInput();
  },{signal});
  canvas.addEventListener('moss-rail-slide',e=>{
    const x=typeof e.detail==='number'?e.detail:e.detail?.x;
    if (!ready || !Number.isFinite(x)) return;
    takeControl();
    worker.postMessage({type:'rail',x});
    updateFramingAfterInput();
  },{signal});
  canvas.addEventListener('moss-palm-drag',e=>{
    const dx=Number(e.detail?.dx), dy=Number(e.detail?.dy);
    if (!ready || !Number.isFinite(dx) || !Number.isFinite(dy) || (!dx && !dy)) return;
    takeControl();
    applyJointDrag(dx, dy);
  },{signal});
  let lastSignal = 0, lastSource, lastPhase, lastFirstVoice;
  canvas.addEventListener('moss-signal',e=>{
    if (e.detail.phase!=='standby') takeControl();
    const now=performance.now();
    const sameState=e.detail.phase===lastPhase && e.detail.source===lastSource && e.detail.firstVoice===lastFirstVoice;
    if (!ready || !visible || document.hidden || (sameState && (lastSource==='idle' || now-lastSignal<45))) return;
    lastSignal=now;
    lastSource=e.detail.source;
    lastPhase=e.detail.phase;
    lastFirstVoice=e.detail.firstVoice;
    worker.postMessage({type:'signal',detail:e.detail});
  },{signal});
  worker.onmessage = async ({data})=>{
    if (signal.aborted) return;
    if (data.type==='load-progress') {
      if (ready || canvas.dataset.error || !loadLabels[data.stage]) return;
      const label = loadLabels[data.stage];
      if (loadLabel.textContent !== label) loadLabel.textContent = label;
      if (data.total > 0) {
        loadMeter.max = data.total;
        loadMeter.value = data.loaded;
      } else loadMeter.removeAttribute('value');
      loadReading.hidden = !['optics','exterior'].includes(data.stage);
      if (!loadReading.hidden) loadReading.value = data.total > 0
        ? Math.floor(100 * data.loaded / data.total) + '%' : (data.loaded / 1048576).toFixed(1) + ' MiB';
    } else if (data.type==='image') {
      try {
        const image = new Image();
        image.src=data.url;
        await image.decode();
        if (signal.aborted) return;
        const decal=new OffscreenCanvas(image.naturalWidth,image.naturalHeight);
        decal.getContext('2d').drawImage(image,0,0);
        const bitmap=decal.transferToImageBitmap();
        worker.postMessage({type:'image',id:data.id,bitmap},[bitmap]);
      } catch(error) {
        if (!signal.aborted) worker.postMessage({type:'image',id:data.id,message:error.message});
      }
    } else if (data.type==='optics') {
      grip=data.grip;
      if (data.subject) panelSpace.updateSubject(data.subject);
      displayOptics(data.optics);
      canvas.dispatchEvent(new CustomEvent('moss-optics',{detail:{...data.optics,grip:data.grip}}));
    } else if (data.type==='startup-state') {
      canvas.dataset.selfTest=data.value;
      checkIndicator.hidden=data.value!=='running';
    } else if (data.type==='pose') {
      activePreset=data.preset;
      camera.userData.mountTop=data.mountTop;
      camera.userData.mountAnchor=data.mountAnchor;
      const {width,height}=readStatus();
      fitMossCamera(camera,width,height,activePreset);
      suppressControlsChange=true;
      try {
        controls.enableDamping=false;
        controls.update();
        controls.target.fromArray(data.target);
        camera.position.fromArray(data.position);
        controls.update();
      } finally {
        controls.enableDamping=true;
        suppressControlsChange=false;
      }
    } else if (data.type==='ready') {
      ready=true; controls.enabled=true;
      canvas.dataset.ready='true';
      canvas.dataset.rendering=data.software?'software':'hardware';
      const release=data.stats.modelRelease;
      canvas.dataset.modelRelease=release.sourceFile;
      canvas.dataset.modelSourceSha256=release.sourceSha256;
      canvas.title='550W 三维模型 · '+release.sourceFile.replace(/\.blend$/i,'');
      maybeStart();
    } else if (data.type==='error') fail(Error(data.message));
  };
  worker.addEventListener('error',e=>fail(Error(e.message)),{signal});
  window.addEventListener('pagehide',e=>{
    if (!e.persisted) lifetime.abort();
  },{signal});
}
export function mountMossView(canvas) {
  const lifetime = new AbortController();
  try {start(canvas,lifetime);} catch(error) {lifetime.abort();failed(canvas,error);}
  return ()=>lifetime.abort();
}

if (new URL(import.meta.url).searchParams.get('lifecycle') !== '1') {
  const canvas = document.getElementById('moss-model');
  if (canvas) mountMossView(canvas);
}
