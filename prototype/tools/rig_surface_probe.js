// Used only by fixed probe meshes with non-indexed CAD positions.
function prepareRigSurfaceProbe(THREE, mesh) {
  const geometry = mesh.geometry;
  const position = geometry.getAttribute("position");
  const range = geometry.drawRange;
  if (geometry.index || Array.isArray(mesh.material) || !position ||
      position.count % 3 || Object.keys(geometry.morphAttributes).length ||
      range.start !== 0 || range.count !== Infinity) {
    return raycaster => raycaster.intersectObject(mesh, false)[0];
  }

  const point = new THREE.Vector3();
  const blockSize = 96 * 3;
  const blocks = [];
  for (let start = 0; start < position.count; start += blockSize) {
    const stop = Math.min(start + blockSize, position.count);
    const bounds = new THREE.Box3();
    for (let vertex = start; vertex < stop; vertex++) {
      bounds.expandByPoint(point.fromBufferAttribute(position, vertex));
    }
    // Pad the bounds around the original Float32 positions.
    bounds.expandByScalar(.0001);
    blocks.push({start, count:stop-start, bounds});
  }

  const localRay = new THREE.Ray();
  const inverseWorld = mesh.matrixWorld.clone().invert();
  const hits = [];
  return raycaster => {
    if (!mesh.layers.test(raycaster.layers)) return undefined;
    localRay.copy(raycaster.ray).applyMatrix4(inverseWorld);
    hits.length = 0;
    try {
      for (const {start, count, bounds} of blocks) {
        if (!localRay.intersectsBox(bounds)) continue;
        range.start = start;
        range.count = count;
        // Keep Three's own winding, UV interpolation, normals and near/far tests.
        mesh.raycast(raycaster, hits);
      }
    } finally {
      range.start = 0;
      range.count = Infinity;
    }
    // Original face order preserves the tie-break for equal distances.
    let closest;
    for (const hit of hits) if (!closest || hit.distance < closest.distance) closest = hit;
    return closest;
  };
}
