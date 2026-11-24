export function microphoneInputs(onChange) {
  const choice=document.getElementById('mic-device');
  const note=document.getElementById('mic-device-status');
  const storeKey='moss.conversation.micDevice';
  let preferred='', active=null, activeMeta=null, revision=0;
  try { preferred=localStorage.getItem(storeKey) || ''; } catch {}

  function commit(id) {
    preferred=id;
    try {
      if (id) localStorage.setItem(storeKey,id);
      else localStorage.removeItem(storeKey);
    } catch {}
  }
  async function refresh() {
    const request=++revision;
    let devices=[];
    try { devices=await navigator.mediaDevices?.enumerateDevices() || []; } catch {}
    if (request!==revision) return;
    const inputs=devices.filter(d=>d.kind==='audioinput' && d.deviceId && d.deviceId!=='default' && d.deviceId!=='communications');
    const options=[new Option('跟随系统默认','')];
    for (const [index,device] of inputs.entries())
      options.push(new Option(device.label || '麦克风 '+(index+1),device.deviceId));
    if (preferred && !inputs.some(d=>d.deviceId===preferred))
      options.push(new Option('上次选择的麦克风',preferred));
    if (choice) {
      choice.replaceChildren(...options);
      choice.value=preferred;
    }
    const label=active?.label || activeMeta?.label || '';
    if (note) note.textContent=label ? '正在使用 '+label : '';
  }
  choice?.addEventListener('change',()=>onChange(choice.value));
  choice?.closest('details')?.addEventListener('toggle',e=>{if (e.currentTarget.open) refresh();});
  navigator.mediaDevices?.addEventListener('devicechange',refresh);
  void refresh();
  return {
    get value(){return preferred;},
    commit,
    setTrack(track, meta=null){ active=track; activeMeta=meta; refresh(); },
    refresh,
  };
}
