import {Vector3} from 'moss-three';

const mountPoint=new Vector3();

// 以同一个倍率调整两个方向的视野。窗口变化时保持观察者和镜头的相对位置。
export function fitMossCamera(camera, width, height, preset) {
  const aspect = width / height;
  const fitAspect = preset === 'interview' || preset === 'whole' ? .63 : 1.1;
  camera.zoom = Math.min(1, aspect / fitAspect);
  camera.aspect = aspect;
  if (preset === 'interview' && Number.isFinite(camera.userData.mountTop)) {
    let top = (1 - camera.zoom) / 2 + camera.userData.mountTop * camera.zoom;
    if (camera.userData.mountAnchor) {
      camera.clearViewOffset();
      camera.updateMatrixWorld();
      mountPoint.fromArray(camera.userData.mountAnchor).applyMatrix4(camera.matrixWorldInverse);
      if (mountPoint.z < -camera.near) {
        mountPoint.applyMatrix4(camera.projectionMatrix);
        top=Math.max(top,(1-mountPoint.y)/2);
      }
    }
    camera.setViewOffset(width,height,0,(top + .008) * height,width,height);
  } else camera.clearViewOffset();
}
