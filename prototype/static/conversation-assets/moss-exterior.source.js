import {stabilizePanelFinish} from './h50-panel-finish.js';

// 发布模型以 CAD 毫米和中立姿态导出。原装配继续提供关节、诊断屏和光学装置。
export function installExterior(THREE, model, tof, scene, glbSha256, expectedSource) {
  const {sourceFile, sourceSha256} = scene.userData;
  if (!sourceFile || sourceSha256 !== expectedSource) throw Error('加载的机身与此版本的来源校验值不符。');
  const rig = model.mechanisms;
  const targets = {mount:model.assembly, shoulder:rig.shoulder, elbow:rig.elbow, head:model.head};
  const groups = [...scene.children];
  const names = new Set(groups.map(group => group.name));
  if (groups.length !== Object.keys(targets).length || !Object.keys(targets).every(key => names.has(key)))
    throw Error('机身与当前机械臂的装配分组不符。');
  const h50PanelTriangles=stabilizePanelFinish(THREE,scene);

  const active = new Set([rig.bottomLED]);
  for (const group of [tof, rig.screen])
    group.traverse(object => { if (object.isMesh) active.add(object); });
  for (const led of rig.auxLEDs) {
    active.add(led.emitter);
    active.add(led.lens);
  }
  const retired = retireOriginalShell(THREE,model,active);
  const parts = {};
  let visibleMeshes = 0, visibleTriangles = 0;
  for (const group of groups) {
    model.assembly.add(group);
    model.assembly.updateWorldMatrix(true, false);
    if (targets[group.name] !== model.assembly) targets[group.name].attach(group);
    let meshes = 0, triangles = 0;
    group.traverseVisible(object => {
      if (!object.isMesh || !object.layers.isEnabled(0)) return;
      const primitive = object.geometry.index || object.geometry.getAttribute('position');
      meshes++;
      triangles += primitive.count / 3;
    });
    if (!triangles) throw Error('机身分组没有可见三角面：' + group.name);
    parts[group.name] = {meshes,triangles};
    visibleMeshes += meshes;
    visibleTriangles += triangles;
  }
  return {sourceFile,sourceSha256,glbSha256,groups:groups.length,parts,visibleMeshes,visibleTriangles,retired,h50PanelTriangles};
}

function retireOriginalShell(THREE,model,active) {
  // 摄影机平移约束读取顶座和两个外壳的边界。保留它们的顶点及所需祖先，外观由新机身绘制。
  const anchors = [model.meshes[0], ...model.parts.filter(p => ['shell','front-shell'].includes(p.id)).map(p => p.object)];
  const keepNodes = new Set();
  for (let object of [...active,...anchors,model.head,model.assembly]) {
    while (object && !keepNodes.has(object)) {keepNodes.add(object); object=object.parent;}
  }
  const materials = object => Array.isArray(object.material) ? object.material : [object.material];
  const textures = material => Object.values(material).filter(value => value?.isTexture);
  const keepGeometry = new Set([...active,...anchors].map(object => object.geometry));
  const keepMaterials = new Set([...active].flatMap(materials));
  const keepTextures = new Set([...keepMaterials].flatMap(textures));
  const geometry = new Set(), surface = new Set(), images = new Set(), objects = [];
  model.root.traverse(object => objects.push(object));
  // 原光圈已由双环 GLB 驱动。不再对拆下的旧叶片每帧计算形变。
  model.mechanisms.irisBlades.length=0;
  model.mechanisms.rotor.traverse(object => objects.push(object));
  const emptyGeometry = new THREE.BufferGeometry();
  let retiredMeshes = 0;
  for (const object of objects) {
    if (!object.isMesh || active.has(object)) continue;
    retiredMeshes++;
    object.layers.disable(0);
    if (!keepGeometry.has(object.geometry)) {geometry.add(object.geometry); object.geometry=emptyGeometry;}
    for (const material of materials(object)) {
      if (keepMaterials.has(material) || surface.has(material)) continue;
      surface.add(material);
      for (const texture of textures(material)) if (!keepTextures.has(texture)) images.add(texture);
    }
  }
  for (const object of objects)
    if (!keepNodes.has(object) && keepNodes.has(object.parent)) object.removeFromParent();
  for (const resource of [...geometry,...surface,...images]) resource.dispose();
  return {meshes:retiredMeshes,geometries:geometry.size,materials:surface.size,textures:images.size};
}
