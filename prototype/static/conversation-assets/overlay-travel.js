// 统一使用画布坐标计算资料窗和机身的位置。
export function observeOverlaySpace(canvas, send) {
  const panels = ['dialogue', 'lore-panel', 'tools-panel'].map(id => document.getElementById(id));
  const stage = canvas.closest('main');
  const stacked = matchMedia('(max-width: 900px) and (orientation: portrait), (max-width: 590px) and (max-height: 490px)');
  let subject;
  let frame = 0, previous = '';
  function dock(panel, canvasRect) {
    if (!panel) {
      delete stage.dataset.projectionDock;
      return;
    }
    if (stacked.matches) {
      const eyeY = clamp(subject?.eyeY ?? .5, 0, 1);
      const chatOpen = !panels[0].hidden;
      const bottom = !chatOpen && eyeY < (stage.dataset.projectionDock === 'bottom' ? .48 : .38);
      stage.dataset.projectionDock = bottom ? 'bottom' : 'top';
      const clearance = Math.max(25,canvasRect.height * .065);
      const room = fraction => Math.round(Math.max(86,fraction * canvasRect.height - clearance - 6)) + 'px';
      stage.style.setProperty('--projection-lens-room',room(bottom ? 1-eyeY : eyeY));
      stage.style.setProperty('--chat-lens-room',room(1-eyeY));
      return;
    }
    const previousSide = stage.dataset.projectionDock;
    let side = previousSide === 'left' ? 'left' : 'right';
    if (subject) {
      const parent = panel.offsetParent.getBoundingClientRect();
      const style = getComputedStyle(panel);
      const left = (parent.left + parseFloat(style.left) - canvasRect.left) / canvasRect.width;
      const right = (parent.right - parseFloat(style.right) - canvasRect.left) / canvasRect.width;
      const width = panel.offsetWidth / canvasRect.width;
      const top = (parent.top + panel.offsetTop - canvasRect.top) / canvasRect.height;
      const bottom = top + panel.offsetHeight / canvasRect.height;
      function cost(start) {
        const end = start + width;
        const eyeGap = Math.max(start - subject.eye, subject.eye - end, 0);
        const bodyOverlap = Math.max(0, Math.min(end,subject.right) - Math.max(start,subject.left));
        const bodyHeight = Math.max(0, Math.min(bottom,subject.bottom) - Math.max(top,subject.top));
        const nearEye = top < subject.eyeY + .06 && bottom > subject.eyeY - .06;
        return bodyOverlap * bodyHeight + (nearEye ? 3 * Math.max(0,.12 - eyeGap) : 0);
      }
      // 两侧可用空间接近时留在当前一侧，防止关节轻微移动引发跳边。
      const difference = cost(left) - cost(right - width);
      if (Math.abs(difference) > .015) side = difference < 0 ? 'left' : 'right';
    }
    if (side !== previousSide) stage.dataset.projectionDock = side;
  }
  function measure() {
    frame = 0;
    const canvasRect = canvas.getBoundingClientRect();
    if (!canvasRect.width || !canvasRect.height) return;
    const projection = panels.slice(1).find(panel => !panel.hidden && panel.offsetWidth);
    dock(projection,canvasRect);
    const rects = [];
    for (const panel of panels) {
      if (panel.hidden || stage.hasAttribute('data-opening') || !panel.offsetWidth) continue;
      // 读取排版位置，以免投影闪现动画带动滑车反复移动。
      const parent = panel.offsetParent.getBoundingClientRect();
      const x = parent.left + panel.offsetLeft - canvasRect.left;
      const y = parent.top + panel.offsetTop - canvasRect.top;
      rects.push([x / canvasRect.width, y / canvasRect.height,
        (x + panel.offsetWidth) / canvasRect.width, (y + panel.offsetHeight) / canvasRect.height]);
    }
    const signature = JSON.stringify([canvasRect.width, canvasRect.height, rects]);
    if (signature !== previous) {previous = signature; send(rects);}
  }
  function schedule() {if (!frame) frame = requestAnimationFrame(measure);}
  const size = new ResizeObserver(schedule), visibility = new MutationObserver(schedule);
  for (const panel of panels) {
    size.observe(panel);
    visibility.observe(panel, {attributes:true, attributeFilter:['hidden']});
  }
  size.observe(canvas);
  visibility.observe(stage, {attributes:true, attributeFilter:['data-opening','data-lore-chat-open']});
  schedule();
  return {
    updateSubject(value) {subject = value; schedule();},
    disconnect() {cancelAnimationFrame(frame); size.disconnect(); visibility.disconnect();},
  };
}

const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

export function createOverlayTravel(THREE, model, camera, eyePosition) {
  // 保存机身顶点的局部坐标，重新取景时再计算关节转动后的投影。
  const corners = [];
  for (const {id, object} of model.parts) {
    if (id !== 'shell' && id !== 'front-shell') continue;
    const geometry = object.geometry;
    if (!geometry.boundingBox) geometry.computeBoundingBox();
    const {min,max} = geometry.boundingBox;
    for (const x of [min.x,max.x]) for (const y of [min.y,max.y]) for (const z of [min.z,max.z])
      corners.push({object, point:new THREE.Vector3(x,y,z)});
  }
  const projection = new THREE.Matrix4();
  let target = 0, offset = 0, lastSide = -1;
  function plan(rects) {
    target = 0;
    camera.updateMatrixWorld();
    projection.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse);
    function clip(world) {
      return new THREE.Vector4(world.x - offset,world.y,world.z,1).applyMatrix4(projection);
    }
    const axis = projection.elements;
    const eye = clip(eyePosition());
    if (eye.w <= camera.near) return;
    const body = corners.map(({object,point}) => clip(object.localToWorld(point.clone()))).filter(p => p.w > camera.near);
    if (!body.length) return;
    const screenX = (p,travel) => .5 + .5 * (p.x + axis[0] * travel) / (p.w + axis[3] * travel);
    const screenY = p => .5 - .5 * p.y / p.w;
    const eyeX = screenX(eye,0);
    const bodyLeft = Math.min(...body.map(p => screenX(p,0)));
    const bodyRight = Math.max(...body.map(p => screenX(p,0)));
    const top = Math.min(...body.map(screenY)), bottom = Math.max(...body.map(screenY));
    footprint = {eye:eyeX,eyeY:screenY(eye),left:bodyLeft,right:bodyRight,top,bottom};
    if (!rects.length) {lastSide = -1; return;}
    const blocked = rects.filter(([l,t,r,b]) => r > 0 && l < 1 && t < Math.min(1,bottom) && b > Math.max(0,top))
      .map(([l,,r]) => [clamp(l-.018,0,1),clamp(r+.018,0,1)]).sort((a,b)=>a[0]-b[0]);
    if (!blocked.length) return;
    let edge = 0;
    const lanes = [];
    for (const [l,r] of blocked) {
      if (l>edge) lanes.push([edge,l]);
      edge=Math.max(edge,r);
    }
    if (edge<1) lanes.push([edge,1]);
    // 手机资料占满整行时，保留原来的镜头位置。
    if (!lanes.length) return;
    function score([left,right]) {
      const side = (left+right)/2 < .5 ? -1 : 1;
      return right-left + (side===lastSide ? .045 : 0);
    }
    lanes.sort((a,b)=>score(b)-score(a));
    const [left,right]=lanes[0];
    if (right-left < .13) return;
    lastSide = (left+right)/2 < .5 ? -1 : 1;
    const bodyCenter=(bodyLeft+bodyRight)/2;
    const desiredEye = clamp((left+right)/2 + clamp(eyeX-bodyCenter,-.12,.12),left+.035,right-.035);
    const ndc = desiredEye*2-1;
    const coefficient=axis[0]-ndc*axis[3];
    const slope=(axis[0]*eye.w-eye.x*axis[3])/(eye.w*eye.w);
    // 从导轨端部观察时横移几乎全在景深方向，保留手动取景。
    if (Math.abs(coefficient)<.001 || Math.abs(slope)*eye.w < .45) return;
    const wanted=(ndc*eye.w-eye.x)/coefficient;
    const minX=Math.min(.02,bodyLeft), maxX=Math.max(.98,bodyRight);
    // 根据视锥限制行程，镜头特写本身就允许机壳部分出画。
    for (let step=20; step>0; step--) {
      const move=wanted*step/20, lens=screenX(eye,move);
      if (eye.w+axis[3]*move <= camera.near || lens<.055 || lens>.945) continue;
      if (body.every(p=>p.w+axis[3]*move>camera.near && screenX(p,move)>=minX-.012 && screenX(p,move)<=maxX+.012)) {
        target=move; break;
      }
    }
  }
  function advance(dt,reduced) {
    offset = reduced ? target : THREE.MathUtils.damp(offset,target,4.8,dt);
    if (Math.abs(offset-target)<.001) offset=target;
    return offset;
  }
  let footprint;
  return {plan,advance,get footprint(){return footprint;},get offset(){return offset;},get target(){return target;}};
}
