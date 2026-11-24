import {drawWaveTrace} from './communication-wave.js';

// 给字幕和音轨共用一块可见区域。只在面板开合、换边或尺寸变化时重新排版。
function placeLane(lane, viewport) {
  const stage = viewport.closest('main');
  const panels = ['dialogue', 'lore-panel', 'tools-panel'].map(id => document.getElementById(id));
  const readouts = [...stage.querySelectorAll('.optic-readout,.scene-gesture')];
  let scheduled = false;
  function measure() {
    scheduled = false;
    const area = viewport.getBoundingClientRect();
    if (!area.width || !area.height) return;
    const boxes = [];
    for (const panel of panels) {
      if (panel.hidden || !panel.offsetWidth) continue;
      const parent = panel.offsetParent.getBoundingClientRect();
      const left = parent.left + panel.offsetLeft - area.left;
      const top = parent.top + panel.offsetTop - area.top;
      boxes.push({left:left-8, right:left+panel.offsetWidth+8, top:top-7, bottom:top+panel.offsetHeight+7});
    }
    boxes.sort((a,b) => a.left-b.left);
    const mobile = matchMedia('(max-width:900px)').matches;
    const bandHeight = mobile ? 98 : 124;
    const gap = Math.ceil(readouts.reduce((space, element) => {
      const rect = element.getBoundingClientRect();
      return rect.height && rect.bottom > area.bottom-bandHeight ? Math.max(space, area.bottom-rect.top+5) : space;
    }, mobile ? 18 : 25));
    const floor = area.height-gap;
    const candidates = [floor, ...boxes.map(b => b.top).filter(y => y < floor && y >= bandHeight)].sort((a,b) => b-a);
    let left = 0, right = area.width, bottom = floor, compact = true;
    for (const y of candidates) {
      let cursor = 0, start = 0, end = 0;
      for (const b of boxes) {
        if (b.bottom <= y-bandHeight || b.top >= y) continue;
        const next = Math.min(area.width, Math.max(0, b.left));
        if (next-cursor > end-start) {start=cursor; end=next;}
        cursor = Math.max(cursor, Math.min(area.width, b.right));
      }
      if (area.width-cursor > end-start) {start=cursor; end=area.width;}
      if (end-start < Math.min(145, area.width*.46)) continue;
      left=start; right=end; bottom=y; compact=false;
      break;
    }
    lane.style.setProperty('--audio-left', Math.round(left)+'px');
    lane.style.setProperty('--audio-right', Math.round(area.width-right)+'px');
    lane.style.setProperty('--audio-gap',gap+'px');
    lane.style.setProperty('--audio-rise', Math.round(floor-bottom)+'px');
    lane.toggleAttribute('data-compact',compact);
  }
  function queue() {
    if (!scheduled) {scheduled=true; requestAnimationFrame(measure);}
  }
  const resize = new ResizeObserver(queue);
  const changed = new MutationObserver(records => {
    if (records.some(r => r.oldValue !== r.target.getAttribute(r.attributeName))) queue();
  });
  resize.observe(viewport);
  for (const element of readouts) resize.observe(element);
  for (const panel of panels) {
    resize.observe(panel);
    changed.observe(panel, {attributes:true, attributeOldValue:true, attributeFilter:['hidden']});
  }
  changed.observe(stage, {attributes:true, attributeOldValue:true, attributeFilter:['data-projection-dock','data-lore-chat-open','data-opening']});
  queue();
}

export function createStageAudio(canvas, viewport) {
  placeLane(canvas.closest('.stage-audio-lane'), viewport);
  const gfx = canvas.getContext('2d');
  let scale = 1, lastDraw = 0, blank = true, lastInput, lastOutput, lastMode;
  new ResizeObserver(([entry]) => {
    scale = Math.min(devicePixelRatio || 1, 2);
    canvas.width = Math.round(entry.contentRect.width*scale);
    canvas.height = Math.round(entry.contentRect.height*scale);
    blank = true;
  }).observe(canvas);

  function spectrum(chart, color, direction) {
    const {width,height} = canvas;
    const bins = chart.bins;
    const rate = chart.source.context.sampleRate;
    const bands = Math.max(1, Math.min(192,Math.floor(width/(4*scale))));
    if (chart.edges?.length !== bands+1 || chart.sampleRate !== rate || chart.binCount !== bins.length) {
      chart.sampleRate = rate;
      chart.binCount = bins.length;
      chart.edges = new Uint16Array(bands+1);
      for (let i=0; i<=bands; i++) {
        chart.edges[i] = Math.min(bins.length-1,Math.floor(80 * 100 ** (i/bands) * bins.length * 2/rate));
      }
    }
    const edges = chart.edges;
    const pitch = width/(edges.length-1);
    const halfHeight = Math.max(0,height-22*scale)*.47;
    if (chart.stageEnvelope?.length !== edges.length-1) chart.stageEnvelope = new Float32Array(edges.length-1);
    gfx.fillStyle = color;
    for (let i=0; i<edges.length-1; i++) {
      let peak = bins[edges[i]];
      for (let j=edges[i]+1; j<edges[i+1]; j++) peak=Math.max(peak,bins[j]);
      const amplitude = peak/255 * halfHeight;
      chart.stageEnvelope[i]=amplitude;
      if (!peak) continue;
      const y = height*.5 - (direction>0 ? 0 : amplitude);
      gfx.fillRect(i*pitch, y, Math.max(1,pitch-scale), amplitude * (direction===0 ? 2 : 1));
    }
  }
  function wave(chart, color, now, reduced) {
    const {width,height} = canvas;
    const peaks = chart.trace.peaks;
    drawWaveTrace(gfx,width,height,peaks,color,scale);
    if (!(chart.dbfs > -55)) return;
    const count = Math.max(2,Math.min(6,Math.floor(width/(120*scale))));
    const phase = reduced ? 0 : now/9000;
    const opacity = gfx.globalAlpha;
    gfx.fillStyle=color;
    for (let i=0; i<count; i++) {
      const index = Math.round(((phase+(i+.4)/count)%1)*(peaks.length-1));
      const x = width*index/(peaks.length-1);
      const y = Math.floor(height/2)+.5-Math.max(-1,Math.min(1,peaks[index]*2))*height*.46;
      gfx.globalAlpha=opacity*.16;
      gfx.beginPath(); gfx.arc(x,y,2*scale,0,Math.PI*2); gfx.fill();
      gfx.globalAlpha=opacity*.86;
      gfx.beginPath(); gfx.arc(x,y,.9*scale,0,Math.PI*2); gfx.fill();
    }
    gfx.globalAlpha=opacity;
  }
  function orbit(charts,input,output,inputColor,outputColor,now,reduced) {
    const {width,height} = canvas;
    const count = Math.min(7,Math.max(3,Math.floor(width/(92*scale))));
    const phase = reduced ? .32 : now/4900;
    for (let i=0; i<count; i++) {
      const angle=phase+i*Math.PI*2/count;
      const sine=Math.sin(angle), depth=.5+.5*sine;
      const useOutput=output && (!input || sine>=0);
      const chart=charts[useOutput ? 1 : 0];
      if (!(chart.dbfs > -58)) continue;
      const color=useOutput ? outputColor : inputColor;
      const strength=Math.min(1,(chart.dbfs+58)/25);
      const position=.5+Math.cos(angle)*.485;
      const band=Math.floor(position*chart.stageEnvelope.length);
      const edge=reduced ? (height-22*scale)*.47 : chart.stageEnvelope[band];
      const lift=Math.min(1,Math.abs(sine)*4.5);
      const offset=(edge+(5.2+1.2*depth)*scale)*Math.sign(sine)*lift;
      const x=width*position, y=height*.5+offset;
      const radius=(1.05+.55*depth+.15*strength)*scale;
      gfx.fillStyle=color;
      gfx.globalAlpha=strength*(.12+.10*depth);
      gfx.beginPath(); gfx.arc(x,y,radius*2.3,0,Math.PI*2); gfx.fill();
      gfx.globalAlpha=strength*(.65+.32*depth);
      if (depth>.43) gfx.fillStyle='#fff';
      gfx.beginPath(); gfx.arc(x,y,radius,0,Math.PI*2); gfx.fill();
    }
    gfx.globalAlpha=1;
  }
  return function draw(now, mode, charts, input, output, inputColor, outputColor, reduceMotion) {
    if (!gfx || !canvas.width || !canvas.height) return;
    if (!input && !output) {
      if (!blank) gfx.clearRect(0,0,canvas.width,canvas.height);
      blank = true;
      return;
    }
    if (document.hidden) return;
    if (reduceMotion && !blank && input === lastInput && output === lastOutput && mode === lastMode && now-lastDraw<1000) return;
    lastInput = input; lastOutput = output; lastMode = mode;
    lastDraw=now; blank=false;
    gfx.clearRect(0,0,canvas.width,canvas.height);
    if (mode==='waveform') {
      if (input) {
        gfx.globalAlpha=output ? .60 : 1;
        wave(charts[0],inputColor,now,reduceMotion);
        gfx.globalAlpha=1;
      }
      if (output) wave(charts[1],outputColor,now,reduceMotion);
    } else {
      if (input) spectrum(charts[0],inputColor,output ? -1 : 0);
      if (output) spectrum(charts[1],outputColor,input ? 1 : 0);
      orbit(charts,input,output,inputColor,outputColor,now,reduceMotion);
    }
  };
}
