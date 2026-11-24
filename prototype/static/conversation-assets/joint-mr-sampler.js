const jointMaterials = new Set([
  'Web 550W / ceramic-white powder-coated aluminium /  · 外观质感.015',
  'Web 使用痕迹 · P04_机械臂2-1@RDK550W装配体',
]);

export function useJointBaseLevel(material, linearFilter, adjusted) {
  if (!jointMaterials.has(material.name) || adjusted.has(material)) return;
  if (!material.metalnessMap || !material.roughnessMap) return;
  // 避免共享贴图的低分辨率层把邻接 UV 岛的金属区域带进这两处关节。
  const clones = new Map();
  for (const key of ['metalnessMap', 'roughnessMap']) {
    const original = material[key];
    if (!clones.has(original)) {
      const image = original.clone();
      image.minFilter = linearFilter;
      image.generateMipmaps = false;
      image.needsUpdate = true;
      clones.set(original, image);
    }
    material[key] = clones.get(original);
  }
  adjusted.add(material);
}
