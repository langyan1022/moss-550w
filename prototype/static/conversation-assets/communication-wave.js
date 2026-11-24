// 每六毫秒记录一对 PCM 正负峰值，按照采样时间从左向右绘制。
const slices = 192;
export function createWaveTrace() {
  return {peaks:new Float32Array(slices * 2), lastSlice:0, source:null};
}

export function sampleTrace(trace, source, samples) {
  const rate = source.context.sampleRate;
  const sliceSize = Math.round(rate * .006);
  const endSample = Math.floor(source.context.currentTime * rate);
  const endSlice = Math.floor(endSample / sliceSize);
  const previous = trace.source === source ? trace.lastSlice : endSlice - slices;
  const count = Math.min(slices, Math.max(0,endSlice - previous));
  if (!count) return;
  const peaks = trace.peaks;
  peaks.copyWithin(0,count*2);
  for (let k=0; k<count; k++) {
    const start = samples.length - endSample + (endSlice - count + k) * sliceSize;
    const output = (slices - count + k) * 2;
    let low=0, high=0, lowAt=0, highAt=0;
    for (let i=Math.max(0,start); i<Math.min(start+sliceSize,samples.length); i++) {
      const value=samples[i];
      if (value<low) {low=value;lowAt=i;}
      if (value>high) {high=value;highAt=i;}
    }
    peaks[output] = lowAt<=highAt ? low : high;
    peaks[output+1] = lowAt<=highAt ? high : low;
  }
  trace.source=source;
  trace.lastSlice=endSlice;
}

export function drawCommunicationWave(chart, source, color, scale) {
  const {gfx,canvas,trace,samples}=chart;
  const {width,height}=canvas;
  if (source) sampleTrace(trace,source,samples);
  else if (trace.source) {trace.source=null; trace.peaks.fill(0);}
  drawWaveTrace(gfx,width,height,source && trace.peaks,color,scale);
}

export function drawWaveTrace(gfx,width,height,peaks,color,scale) {
  if (!width || !height) return;
  const center=Math.floor(height/2)+.5;
  gfx.lineWidth=Math.max(1,scale);
  gfx.strokeStyle=color;
  gfx.beginPath();
  if (peaks) {
    for (let i=0;i<peaks.length;i++) {
      const x=i*width/(peaks.length-1);
      const y=center - Math.max(-1,Math.min(1,peaks[i]*2))*(height*.46);
      if (i===0) gfx.moveTo(x,y); else gfx.lineTo(x,y);
    }
  } else {
    gfx.moveTo(0,center);gfx.lineTo(width,center);
  }
  gfx.stroke();
}
