const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

function pointsUp(points) {
  if (points?.length !== 21) return false;
  const wrist = points[0];
  const palm = distance(wrist, points[9]);
  if (palm < .025) return false;
  const reach = (joint, tip) => distance(wrist, points[tip]) / Math.max(.001, distance(wrist, points[joint]));
  return reach(6, 8) > 1.15 && (points[6].y - points[8].y) / palm > .22 &&
    reach(10, 12) < 1.05 && reach(14, 16) < 1.05 && reach(18, 20) < 1.05;
}

export function installHandGesture({toggle, status, video, canvas}) {
  if (!toggle || !status || !video || !canvas) return;
  const stage = canvas.closest("main");
  const views = [...stage.querySelectorAll('.camera-options input[name="camera-view"]')];
  const requestCamera = navigator.mediaDevices?.getUserMedia?.bind(navigator.mediaDevices);
  const assets = "/conversation-assets/hand-gesture";
  const sceneReady = () => canvas.dataset.ready === "true" && views.length > 1 &&
    !stage.hasAttribute("data-opening") && !stage.hasAttribute("data-light-scene");
  const idleMessage = "开启后用本机摄像头识别手势。";
  let current = null;
  let starting = false;
  let wasAvailable = false;
  toggle.checked = false;
  video.muted = video.playsInline = true;

  function stop(message = "摄像头已关闭。") {
    const session = current;
    current = null;
    toggle.checked = false;
    if (session) {
      clearTimeout(session.frame);
      session.unwatchPermission?.();
      session.stream?.getTracks().forEach(track => track.stop());
      video.pause();
      video.srcObject = null;
      // 摄像头需先释放，识别器关闭出错也不能阻断页面控件。
      try { session.tracker?.close(); } catch {}
    }
    status.textContent = message;
  }

  function refresh() {
    const unavailable = !requestCamera ? "当前浏览器无法使用摄像头。" :
      !sceneReady() ? "三维场景就绪后可开启手势。" : "";
    toggle.disabled = Boolean(unavailable);
    if (unavailable) stop(unavailable);
    else if (!wasAvailable) status.textContent = idleMessage;
    wasAvailable = !unavailable;
  }

  async function watchPermission(session) {
    // 使用轨道的 ended 作为通用通知，支持 Permissions API 时也处理显式撤权。
    try {
      const permission = await navigator.permissions?.query({name:"camera"});
      if (!permission || current !== session) return;
      const changed = () => {
        if (current === session && permission.state !== "granted") stop("摄像头权限已撤销。");
      };
      permission.addEventListener("change", changed);
      session.unwatchPermission = () => permission.removeEventListener("change", changed);
      if (permission.state === "denied") changed();
    } catch {}
  }

  async function start() {
    if (current) return;
    if (!requestCamera || !sceneReady() || document.hidden) { toggle.checked = false; return; }
    if (starting) { stop("上次的手势启动仍在结束，请稍后再开启。"); return; }
    const session = {latched:false, pointSince:null, releaseSince:null, mediaTime:-1, lastSample:0};
    current = session;
    starting = true;
    status.textContent = "正在载入本机手势识别。";
    try {
      const {FilesetResolver, HandLandmarker} = await import(assets + "/vision_bundle.mjs");
      if (current !== session) return;
      const files = await FilesetResolver.forVisionTasks(assets + "/wasm");
      if (current !== session) return;
      const tracker = await HandLandmarker.createFromOptions(files, {
        baseOptions:{modelAssetPath:assets + "/hand_landmarker.task", delegate:"CPU"},
        runningMode:"VIDEO", numHands:1
      });
      if (current !== session) { tracker.close(); return; }
      session.tracker = tracker;
      status.textContent = "等待摄像头授权。";
      const stream = await requestCamera({
        audio:false, video:{width:{ideal:480}, height:{ideal:360}, frameRate:{ideal:8, max:12}}
      });
      if (current !== session) { stream.getTracks().forEach(track => track.stop()); return; }
      session.stream = stream;
      const track = stream.getVideoTracks()[0];
      if (!track || track.readyState !== "live") { stop("摄像头已断开。"); return; }
      track.addEventListener("ended", () => {
        if (current === session) stop("摄像头已断开。");
      }, {once:true});
      void watchPermission(session);
      video.srcObject = stream;
      await video.play();
      if (current !== session) return;
      status.textContent = "竖起食指切换机位，收手后可再次切换。";

      function frame() {
        if (current !== session) return;
        if (document.hidden || track.readyState !== "live") { stop(); return; }
        if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && video.currentTime !== session.mediaTime) {
          const now = performance.now();
          if (now - session.lastSample > 800) session.pointSince = session.releaseSince = null;
          session.lastSample = now;
          session.mediaTime = video.currentTime;
          try {
            if (pointsUp(tracker.detectForVideo(video, now).landmarks[0])) {
              session.releaseSince = null;
              session.pointSince ??= now;
              if (!session.latched && now - session.pointSince >= 350) {
                session.latched = true;
                const index = views.findIndex(item => item.checked);
                const next = views[(index + 1) % views.length];
                next.click();
                status.textContent = `已切换至${next.labels[0].textContent.trim()}机位。收手后可再次切换。`;
              }
            } else {
              session.pointSince = null;
              session.releaseSince ??= now;
              if (now - session.releaseSince >= 450) session.latched = false;
            }
          } catch {
            stop("手势识别中断，摄像头已关闭。");
            return;
          }
        }
        session.frame = setTimeout(frame, 115);
      }
      frame();
    } catch (error) {
      if (current !== session) return;
      stop(error.name === "NotAllowedError" ? "摄像头未获授权，手势已关闭。" : "无法启动手势识别，摄像头已关闭。");
    } finally {
      starting = false;
    }
  }

  toggle.addEventListener("change", () => toggle.checked ? start() : stop());
  document.addEventListener("visibilitychange", () => { if (document.hidden && current) stop(); });
  document.addEventListener("freeze", () => { if (current) stop(); });
  window.addEventListener("pagehide", () => { if (current) stop(); });
  const readiness = new MutationObserver(refresh);
  readiness.observe(canvas, {attributes:true, attributeFilter:["data-ready"]});
  readiness.observe(stage, {attributes:true, attributeFilter:["data-opening","data-light-scene"]});
  refresh();
  return replacement => {
    readiness.disconnect();
    canvas = replacement;
    readiness.observe(canvas, {attributes:true, attributeFilter:["data-ready"]});
    readiness.observe(stage, {attributes:true, attributeFilter:["data-opening","data-light-scene"]});
    refresh();
  };
}
