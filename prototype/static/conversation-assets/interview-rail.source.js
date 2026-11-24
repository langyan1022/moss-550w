export function createInterviewRail(THREE, model) {
  const root = new THREE.Group();
  root.name = '550W translation axis';
  root.add(model.root);
  // 吊装顶座位于活动关节上方，整座只随导轨沿世界 X 轴移动。
  const bounds = new THREE.Box3().setFromObject(model.meshes[0], true);
  bounds.union(new THREE.Box3().setFromObject(model.meshes[1], true));
  const corners = [];
  for (const x of [bounds.min.x,bounds.max.x])
    for (const y of [bounds.min.y,bounds.max.y])
      for (const z of [bounds.min.z,bounds.max.z]) corners.push(new THREE.Vector3(x,y,z));
  const center = bounds.getCenter(new THREE.Vector3());
  const clip = new THREE.Vector4(), projection = new THREE.Matrix4();
  function grip(camera,includeOutside=false) {
    camera.updateMatrixWorld();
    projection.multiplyMatrices(camera.projectionMatrix,camera.matrixWorldInverse);
    let left=Infinity, right=-Infinity, top=Infinity, bottom=-Infinity;
    for (const p of corners) {
      clip.set(p.x+root.position.x,p.y,p.z,1).applyMatrix4(projection);
      if (clip.w <= camera.near) return null;
      const x=.5+.5*clip.x/clip.w, y=.5-.5*clip.y/clip.w;
      left=Math.min(x,left); right=Math.max(x,right);
      top=Math.min(y,top); bottom=Math.max(y,bottom);
    }
    if (!includeOutside && (left>=1 || right<=0 || top>=1 || bottom<=0)) return null;
    clip.set(center.x+root.position.x,center.y,center.z,1).applyMatrix4(projection);
    return {left,right,top,bottom,anchor:.5+.5*clip.x/clip.w};
  }
  function slideTo(camera,x) {
    const handle=grip(camera,true);
    if (!handle || !Number.isFinite(x)) return root.position.x;
    // 留出可再次抓取的边缘，根据当前机位的透视投影反求导轨位置。
    const limit=.02;
    const screen=THREE.MathUtils.clamp(x,Math.min(.48,handle.anchor-handle.left+limit),
      1-Math.min(.48,handle.right-handle.anchor+limit));
    const ndc=2*screen-1;
    const axis=projection.elements;
    const divisor=axis[0]-ndc*axis[3];
    if (Math.abs(divisor)<.001) return root.position.x;
    const delta=(ndc*clip.w-clip.x)/divisor;
    return clip.w+axis[3]*delta>camera.near ? root.position.x+delta : root.position.x;
  }
  function setTravel(position) { root.position.x = position; }
  return {root, grip, slideTo, setTravel};
}
