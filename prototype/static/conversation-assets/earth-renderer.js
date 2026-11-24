const rad = Math.PI / 180;
const scale = 1.28;
const cameraDistance = 1700 / (scale * 500);
const focalLength = .5 / Math.tan(25 * rad);
const edgeRadius = focalLength / Math.sqrt(cameraDistance * cameraDistance - 1);

function point(latitude, longitude) {
  const lat = latitude * rad, lon = longitude * rad;
  const c = Math.cos(lat);
  return [-c * Math.cos(lon), Math.sin(lat), c * Math.sin(lon)];
}

function makeGrid() {
  const lines = [];
  for (let lon = -180; lon < 180; lon += 30) {
    const line = [];
    for (let lat = -90; lat <= 90; lat += 4) line.push(point(lat,lon));
    lines.push(line);
  }
  for (let lat = -60; lat <= 60; lat += 30) {
    const line = [];
    for (let lon = -180; lon <= 180; lon += 4) line.push(point(lat,lon));
    lines.push(line);
  }
  return lines;
}

let encomLoad;
function loadENCOM() {
  if (!encomLoad) {
    encomLoad = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = new URL('./encom-globe/encom-globe.min.js', import.meta.url);
      script.onload = () => {
        script.remove();
        if (window.ENCOM?.Globe) resolve(window.ENCOM.Globe);
        else reject(new Error('地球渲染器载入失败'));
      };
      script.onerror = () => {
        script.remove();
        reject(new Error('地球渲染器载入失败'));
      };
      document.head.append(script);
    }).catch(error => { encomLoad = null; throw error; });
  }
  return encomLoad;
}

export async function createEarthRenderer(stage, signal) {
  signal?.throwIfAborted();
  const [Globe, tiles] = await Promise.all([
    loadENCOM(),
    fetch(new URL('./encom-globe/tiles.json', import.meta.url), {signal}).then(async response => {
      if (!response.ok) throw new Error('地球地图载入失败');
      return (await response.json()).tiles;
    })
  ]);
  signal?.throwIfAborted();
  const overlay = stage.querySelector('canvas');
  stage.style.setProperty('--earth-radius',100*edgeRadius+'%');
  const ctx = overlay.getContext('2d');
  const swatchCanvas = document.createElement('canvas');
  swatchCanvas.width = swatchCanvas.height = 1;
  const swatch = swatchCanvas.getContext('2d',{willReadFrequently:true});
  const grid = makeGrid();
  let globe, land, side = 0, dpr = 1, disposed = false;
  let landColor, gridColor, oceanColor;
  function colors() {
    const style = getComputedStyle(stage);
    function hex(cssColor) {
      swatch.fillStyle=cssColor;
      swatch.fillRect(0,0,1,1);
      return '#'+[...swatch.getImageData(0,0,1,1).data.slice(0,3)].map(v=>v.toString(16).padStart(2,'0')).join('');
    }
    gridColor = hex(style.color);
    landColor = hex(style.borderTopColor);
    oceanColor = hex(style.backgroundColor);
  }
  colors();
  function shadeLand() {
    const mesh=globe.hexGrid, shader=mesh.material;
    const rgb=[1,3,5].map(i=>parseInt(landColor.slice(i,i+2),16)/255);
    const fog=[1,3,5].map(i=>(parseInt(oceanColor.slice(i,i+2),16)/255).toFixed(4));
    const colors=mesh.geometry.attributes.color.array;
    for (let i=0;i<colors.length;i+=3) {
      const tile=Math.floor(i/36);
      const tone=.87+.13*((tile*23)%17)/16;
      for (let j=0;j<3;j++) colors[i+j]=rgb[j]*tone;
    }
    mesh.geometry.attributes.color.needsUpdate=true;
    shader.vertexShader=shader.vertexShader.replace('vColor = vec4( color, opacityVal );',
      'vec3 n = normalize(mat3(modelViewMatrix) * position);' +
      'float light = max(0.0, dot(n, normalize(vec3(-0.55,0.55,0.75))));' +
      'vColor = vec4(color * (0.44 + 0.56 * light), opacityVal);');
    shader.fragmentShader=shader.fragmentShader.replace('vec3 fogColor = vec3(0.0);','vec3 fogColor = vec3('+fog.join(',')+');');
    shader.needsUpdate=true;
  }

  function releaseGlobe() {
    if (!globe) return;
    const old = globe;
    globe = null;
    old.active = false;
    old.domElement.removeEventListener('webglcontextlost', contextLost);
    const context = old.renderer.getContext();
    old.renderer.dispose?.();
    if (context && !context.isContextLost()) context.getExtension('WEBGL_lose_context')?.loseContext();
    old.domElement.remove();
    old.domElement.width = old.domElement.height = 0;
  }

  function fallback() {
    releaseGlobe();
    // 同一份 ENCOM 陆地六边形在没有 WebGL 的设备上以中心点投影。
    land = tiles.map(tile => {
      const p = tile.b.reduce((sum,v) => [sum[0]+v.x,sum[1]+v.y,sum[2]+v.z],[0,0,0]);
      const len = Math.hypot(...p);
      return p.map(v => v/len);
    });
    stage.dataset.renderer = 'encom-canvas2d';
  }
  function contextLost(event) {
    event.preventDefault();
    if (disposed) return;
    fallback();
    stage.dispatchEvent(new Event('earth-redraw'));
  }
  try {
    globe = new Globe(192,192,{tiles, data:[], scale, dayLength:Infinity, viewAngle:.42,
      font:'Consolas',baseColor:landColor, introLinesCount:1, introLinesDuration:1,
      introLinesAltitude:1, introLinesColor:landColor, maxMarkers:1, maxPins:1});
    if (!globe.renderer.getContext()) throw new Error('WebGL 不可用');
    globe.init(()=>{});
    shadeLand();
    globe.firstRenderDate=new Date(Date.now()-1000);
    globe.renderer.setClearColor(oceanColor,1);
    globe.domElement.className='earth-terrain';
    globe.domElement.setAttribute('aria-hidden','true');
    stage.prepend(globe.domElement);
    stage.dataset.renderer='encom-webgl';
    globe.domElement.addEventListener('webglcontextlost', contextLost, {once:true});
  } catch { fallback(); }

  function resize() {
    const size=Math.round(stage.getBoundingClientRect().width);
    const ratio=Math.min(window.devicePixelRatio || 1,2);
    if (!size || (size===side && ratio===dpr)) return;
    side=size; dpr=ratio;
    overlay.width=overlay.height=Math.round(side*dpr);
    if (globe) {
      globe.renderer.devicePixelRatio=dpr;
      globe.renderer.setSize(side,side,false);
    }
  }
  function setTheme() {
    if (disposed) return;
    const previous=landColor;
    colors();
    if (!globe) return;
    if (previous!==landColor) {
      const oldMesh=globe.hexGrid;
      globe.setBaseColor(landColor);
      shadeLand();
      oldMesh.geometry.dispose(); oldMesh.material.dispose();
    }
    globe.renderer.setClearColor(oceanColor,1);
  }

  function draw(latitude, longitude, pin) {
    if (disposed) return;
    resize();
    if (!side) return;
    if (globe) {
      globe.cameraAngle=Math.PI-longitude*rad;
      globe.viewAngle=latitude*rad;
      globe.tick();
    }
    const lat=latitude*rad, lon=longitude*rad;
    const a=Math.cos(lon), b=Math.sin(lon), c=Math.cos(lat), d=Math.sin(lat);
    function project([x,y,z]) {
      const depth=-x*a*c + y*d + z*b*c;
      if (depth < 1 / cameraDistance) return null;
      const magnify=side*focalLength/(cameraDistance-depth);
      return [side*.5+(x*b+z*a)*magnify,side*.5-(x*a*d+y*c-z*b*d)*magnify,depth];
    }
    ctx.setTransform(overlay.width/side,0,0,overlay.height/side,0,0);
    ctx.clearRect(0,0,side,side);
    const radius=side*edgeRadius;
    if (land) {
      ctx.fillStyle=oceanColor;
      ctx.beginPath(); ctx.arc(side/2,side/2,radius,0,2*Math.PI); ctx.fill();
      ctx.fillStyle=landColor;
      for (const tile of land) {
        const p=project(tile);
        if (!p) continue;
        const dot=Math.max(.4,side*.006*p[2]);
        ctx.fillRect(p[0]-dot/2,p[1]-dot/2,dot,dot);
      }
    }
    const shade=ctx.createRadialGradient(side*.34,side*.31,0,side*.54,side*.54,radius);
    shade.addColorStop(0,'rgba(190,230,231,.11)');
    shade.addColorStop(.58,'rgba(50,105,118,.025)');
    shade.addColorStop(1,'rgba(0,6,10,.24)');
    ctx.fillStyle=shade;
    ctx.beginPath();ctx.arc(side/2,side/2,radius,0,2*Math.PI);ctx.fill();
    ctx.strokeStyle=gridColor;
    ctx.globalAlpha=.3;
    ctx.lineWidth=side<85 ? .26 : .55;
    ctx.beginPath();
    for (const line of grid) {
      let pen=false;
      for (const vertex of line) {
        const p=project(vertex);
        if (!p) { pen=false; continue; }
        if (pen) ctx.lineTo(p[0],p[1]); else ctx.moveTo(p[0],p[1]);
        pen=true;
      }
    }
    ctx.stroke();
    ctx.globalAlpha=.53;
    ctx.beginPath(); ctx.arc(side/2,side/2,radius,0,2*Math.PI); ctx.stroke();
    ctx.globalAlpha=1;
    const marker = pin && project(point(pin.latitude,pin.longitude));
    if (marker) {
      const r=side<85?2.6:4.2;
      ctx.lineWidth=1;
      ctx.strokeStyle='#ffd078'; ctx.fillStyle='#ffd078';
      ctx.beginPath(); ctx.arc(marker[0],marker[1],r,0,Math.PI*2); ctx.stroke();
      ctx.beginPath(); ctx.arc(marker[0],marker[1],r*.34,0,Math.PI*2); ctx.fill();
    }
    stage.toggleAttribute('data-location-visible',Boolean(marker));
  }
  function dispose() {
    if (disposed) return;
    disposed = true;
    releaseGlobe();
    land = null;
    overlay.width = overlay.height = 0;
    delete stage.dataset.renderer;
    stage.removeAttribute('data-location-visible');
    stage.style.removeProperty('--earth-radius');
  }
  return {draw, setTheme, dispose};
}
