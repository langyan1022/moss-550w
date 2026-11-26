/**
 * MOSS backend API bridge and opt-in scenario simulation.
 *
 * - Live mode: connects to SSE /api/events?wake=1, polls /api/device-status every 5s,
 *   fetches /api/models, /api/notes, /fui/local-weather, and dispatches every MOSS action.
 * - Simulation activates from the settings control, ?sim=1, or ?beat=<id>.
 */
import { createFeed, createScenario, seeded, wander } from '../vendor/fui-sim.js';

const GIB = 1024 ** 3;

const SIM_PERSONALITIES = [
  {
    id: 'partner',
    code: 'PARTNER',
    label: '熟人搭档',
    label_en: 'Partner',
    description: '自然有人情味、接得住情绪也办得了实事（默认）',
    description_en: 'Natural, warm, and grounded like a trusted partner (default)',
    sample: '累坏了吧？先别硬撑着，喝口水歇会儿再说。',
  },
  {
    id: 'rational',
    code: '550W',
    label: '550W 绝对理性',
    label_en: '550W Rational',
    description: '流浪地球电影风：冷静克制、逻辑精准、极简不煽情',
    description_en: 'Film-grade 550W: calm, analytical, concise, zero sentimentality',
    sample: '已记录你的疲劳状态。建议暂停当前任务十五分钟，恢复认知效率。',
  },
  {
    id: 'lively',
    code: 'LIVELY',
    label: '鲜活热络',
    label_en: 'Lively',
    description: '松弛机灵、接梗自然、更有陪伴感和生活气息',
    description_en: 'Upbeat, witty, conversational, and expressive',
    sample: '哎呀可算扛完这波了！先瘫会儿，想听首轻快的歌放松下不？',
  },
  {
    id: 'sassy',
    code: 'SASSY',
    label: '傲娇嘴硬',
    label_en: 'Cheeky & Snarky',
    description: '嘴硬心热、爱抬杠接梗、被夸还傲娇，正事半步不掉链子',
    description_en: 'Playful back-talker with a heart of gold — witty ribbing without dropping the ball',
    sample: '行吧，看在你求得这么没诚意的份上，这事儿我顺手给你办了，别太感动啊。',
  },
];

export const BEATS = [
  { id: 'nominal',    phase: 'standby',   cpu: 24, ramAvailGb: 20.4, bat: 96, diskFreeGb: 84.2, ac: true,  charging: false },
  { id: 'listening',  phase: 'listening', cpu: 31, ramAvailGb: 19.8, bat: 95, diskFreeGb: 84.2, ac: true,  charging: false },
  { id: 'thinking',   phase: 'thinking',  cpu: 58, ramAvailGb: 17.6, bat: 94, diskFreeGb: 84.1, ac: true,  charging: false },
  { id: 'speaking',   phase: 'speaking',  cpu: 42, ramAvailGb: 18.2, bat: 94, diskFreeGb: 84.1, ac: true,  charging: false },
  { id: 'degraded',   phase: 'standby',   cpu: 88, ramAvailGb: 3.4,  bat: 18, diskFreeGb: 24.5, ac: false, charging: false, warnDraft: true },
  { id: 'critical',   phase: 'standby',   cpu: 97, ramAvailGb: 1.8,  bat: 9,  diskFreeGb: 8.4,  ac: false, charging: false, linkLost: true },
];

export function createMossApi({
  onState,
  onDevice,
  onModels,
  onLink,
  onLog,
  onWakeOffer,
  onBeat,
  onError,
}) {
  const qs = new URLSearchParams(location.search);
  const forcedSim = qs.get('sim') === '1' || qs.has('beat');
  let mode = forcedSim ? 'simulated' : 'connecting'; // 'live' | 'simulated' | 'connecting' | 'offline'
  let currentState = { revision: -1, chat_id: '', turns: [], chats: [], memories: [], timers: [], model: 'DeepSeek', model_id: 'deepseek', personality_id: 'partner', personalities: SIM_PERSONALITIES };
  let latestDevice = null;
  let eventSource = null;
  let runtimeLogSource = null;
  let deviceTimer = 0;
  let reconnectTimer = 0;
  let streamInitial = true;
  let paused = qs.get('paused') === '1';

  /* ── Deterministic simulated state for offline / storyboard QA ── */
  const random = seeded(550);
  let simRevision = 1;
  let simHw = { cpu: 24, ramAvailGb: 20.4, ramTotalGb: 32.0, bat: 96, diskFreeGb: 84.2, diskTotalGb: 238.0, ac: true, charging: false };
  let simBeat = BEATS[0];
  let simTurns = [];

  function buildSimTurns(beat) {
    const now = Date.now();
    const base = [
      {
        id: 'sim-turn-1',
        chat_id: 'sim-chat-01',
        created_at: new Date(now - 95000).toISOString(),
        replied_at: new Date(now - 93000).toISOString(),
        user: '现在几点、电脑还有多少电、音量多大？',
        status: 'replied',
        source: 'context',
        reply: '本机时间 08:24:12。电量 96%（外接供电）。系统音量 64%。',
        local_overview: [
          { kind: 'clock', reply: '2026年11月22日，星期日，08:24:12。' },
          { kind: 'battery', reply: '电量 96%（已接电）。' },
          { kind: 'volume', reply: '系统音量为 64%。' },
        ],
        cloud_requests: { total: 0, by_provider: {} },
      },
      {
        id: 'sim-turn-2',
        chat_id: 'sim-chat-01',
        created_at: new Date(now - 42000).toISOString(),
        replied_at: new Date(now - 40000).toISOString(),
        user: '明天上海天气怎么样？',
        status: 'replied',
        source: 'weather',
        reply: '上海明天（2026-11-23）局部多云，最低 11 °C，最高 18 °C，降水概率 20%。',
        weather: {
          source: 'Open-Meteo',
          city: '上海',
          location_kind: 'city',
          latitude: 31.2304,
          longitude: 121.4737,
          date: '2026-11-23',
          condition: '局部多云',
          temperature_min: 11,
          temperature_max: 18,
          precipitation_probability: 20,
          wind_speed: 14.2,
          days: [
            { date: '2026-11-22', code: 1, condition: '大致晴朗', temperature_min: 10, temperature_max: 19, precipitation_probability: 10 },
            { date: '2026-11-23', code: 2, condition: '局部多云', temperature_min: 11, temperature_max: 18, precipitation_probability: 20 },
            { date: '2026-11-24', code: 61, condition: '小雨', temperature_min: 9, temperature_max: 15, precipitation_probability: 65 },
          ],
        },
        cloud_requests: { total: 0, by_provider: {} },
      },
    ];
    if (beat.id === 'thinking') {
      base.push({
        id: 'sim-turn-3',
        chat_id: 'sim-chat-01',
        created_at: new Date(now - 2000).toISOString(),
        user: '检查本机音频链路和媒体库索引状态。',
        status: 'thinking',
        partial_reply: '正在汇总音频通道电平与本地曲库索引…',
      });
    } else if (beat.id === 'speaking') {
      base.push({
        id: 'sim-turn-3',
        chat_id: 'sim-chat-01',
        created_at: new Date(now - 4000).toISOString(),
        replied_at: new Date(now - 1000).toISOString(),
        user: '朗诵王之涣的《登鹳雀楼》。',
        status: 'replied',
        reply: '白日依山尽，黄河入海流。欲穷千里目，更上一层楼。',
        cloud_requests: { total: 1, by_provider: { deepseek: 1 } },
      });
    } else if (beat.warnDraft) {
      base.push({
        id: 'sim-turn-3',
        chat_id: 'sim-chat-01',
        created_at: new Date(now - 5000).toISOString(),
        user: '帮我保存一份本地草稿，内容逐字写：高负载排查与散热核对清单',
        status: 'pending',
        preview: '高负载排查与散热核对清单',
      });
    } else if (beat.linkLost) {
      base.push({
        id: 'sim-turn-3',
        chat_id: 'sim-chat-01',
        created_at: new Date(now - 3000).toISOString(),
        user: '同步长期记忆并刷新媒体索引。',
        status: 'error',
        error: '本机服务 127.0.0.1:18221 暂不可达，等待重新连接。',
      });
    }
    return base;
  }

  function emitSimSnapshot(time = new Date(), { initial = false } = {}) {
    simRevision += 1;
    const state = {
      revision: simRevision,
      chat_id: 'sim-chat-01',
      agent_mode: 'holojarvis',
      model_id: currentState.model_id || 'deepseek',
      model: currentState.model || 'DeepSeek-V3',
      personality_id: currentState.personality_id || 'partner',
      personalities: SIM_PERSONALITIES,
      memory_backend: 'sqlite',
      chats: [
        { id: 'sim-chat-01', title: '550W 日常控制台巡检' },
        { id: 'sim-chat-02', title: '上海天气与出行核对' },
      ],
      memories: currentState.memories?.length
        ? currentState.memories
        : [
            { id: 'mem-01', text: '默认称呼操作员，优先中文简洁回复。', source: 'panel', created_at: '2026-11-20T09:00:00Z' },
            { id: 'mem-02', text: '本机曲库目录位于 G:\\音乐。', source: 'chat', created_at: '2026-11-21T14:20:00Z' },
          ],
      timers: currentState.timers?.length
        ? currentState.timers
        : [
            { id: 1, label: '核对 550W 本地语音待机内存余量', due_at: Math.floor(Date.now() / 1000) + 1800, chat_id: 'sim-chat-01' },
          ],
      turns: simTurns,
    };
    currentState = state;
    onState?.(state, { simulated: true, initial });

    const device = {
      cpu_percent: Math.round(simHw.cpu),
      memory: { available: Math.round(simHw.ramAvailGb * GIB), total: Math.round(simHw.ramTotalGb * GIB) },
      battery: { present: true, percent: Math.round(simHw.bat), ac: simHw.ac, charging: simHw.charging },
      disk: { drive: 'C:', free: Math.round(simHw.diskFreeGb * GIB), total: Math.round(simHw.diskTotalGb * GIB) },
      sampled_at: time.toISOString(),
    };
    latestDevice = device;
    onDevice?.(device, { simulated: true });
  }

  const simFeed = createFeed({
    hz: 1,
    paused,
    onSample(time) {
      if (mode !== 'simulated') return;
      if (simBeat.id === 'nominal' || simBeat.id === 'listening' || simBeat.id === 'speaking') {
        simHw.cpu = wander(simHw.cpu, random, { min: Math.max(8, simBeat.cpu - 6), max: Math.min(72, simBeat.cpu + 6), step: 2.5 });
        simHw.ramAvailGb = wander(simHw.ramAvailGb, random, { min: 15.0, max: 23.0, step: 0.2 });
      }
      const device = {
        cpu_percent: Math.round(simHw.cpu),
        memory: { available: Math.round(simHw.ramAvailGb * GIB), total: Math.round(simHw.ramTotalGb * GIB) },
        battery: { present: true, percent: Math.round(simHw.bat), ac: simHw.ac, charging: simHw.charging },
        disk: { drive: 'C:', free: Math.round(simHw.diskFreeGb * GIB), total: Math.round(simHw.diskTotalGb * GIB) },
        sampled_at: time.toISOString(),
      };
      latestDevice = device;
      onDevice?.(device, { simulated: true });
    },
  });

  let scenario = null;
  let scenarioInitialized = false;

  function initScenario() {
    if (scenario) return scenario;
    scenario = createScenario({
      beats: BEATS,
      keys: forcedSim,
      apply(beat) {
        const isFirstInit = !scenarioInitialized;
        scenarioInitialized = true;
        simBeat = beat;
        simHw = {
          cpu: beat.cpu,
          ramAvailGb: beat.ramAvailGb,
          ramTotalGb: 32.0,
          bat: beat.bat,
          diskFreeGb: beat.diskFreeGb,
          diskTotalGb: 238.0,
          ac: beat.ac,
          charging: beat.charging,
        };
        simTurns = buildSimTurns(beat);
        if (mode === 'simulated' && !isFirstInit) {
          emitSimSnapshot(new Date(), { initial: false });
          onBeat?.(beat);
          onLink?.({
            mode: 'simulated',
            online: !beat.linkLost,
            beatId: beat.id,
          });
          const lvl = beat.id === 'critical' ? 'crit' : beat.id === 'degraded' ? 'warn' : beat.id === 'nominal' ? 'ok' : 'info';
          onLog?.({ time: Date.now(), source: 'SIM', text: `Beat → ${beat.id.toUpperCase()}`, level: lvl });
        }
      },
    });
    return scenario;
  }

  function enterSimulated(beat = simBeat) {
    stopLiveStreams();
    mode = 'simulated';
    simBeat = beat;
    simHw = {
      cpu: beat.cpu,
      ramAvailGb: beat.ramAvailGb,
      ramTotalGb: 32.0,
      bat: beat.bat,
      diskFreeGb: beat.diskFreeGb,
      diskTotalGb: 238.0,
      ac: beat.ac,
      charging: beat.charging,
    };
    simTurns = buildSimTurns(beat);
    onModels?.({
      selected: 'deepseek',
      models: [
        { id: 'deepseek', label: 'DeepSeek', model: 'deepseek-chat', available: true },
        { id: 'aliyun', label: '百炼 Qwen', model: 'qwen-plus', available: true },
        { id: 'kimi', label: 'Kimi', model: 'moonshot-v1-8k', available: true },
        { id: 'local', label: '本机 Qwen3.5-4B', model: 'moss-chat-qwen35-4b', available: true },
      ],
    });
    emitSimSnapshot(new Date(), { initial: true });
    onBeat?.(beat);
    onLink?.({ mode: 'simulated', online: !beat.linkLost, beatId: beat.id });
    if (!paused) simFeed.resume();
  }

  function stopLiveStreams() {
    if (eventSource) { eventSource.close(); eventSource = null; }
    if (runtimeLogSource) { runtimeLogSource.close(); runtimeLogSource = null; }
    clearInterval(deviceTimer);
    deviceTimer = 0;
    clearTimeout(reconnectTimer);
    reconnectTimer = 0;
  }

  async function pollDeviceStatus() {
    if (paused || document.hidden || mode !== 'live') return;
    try {
      const resp = await fetch('/api/device-status', { cache: 'no-store' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      latestDevice = data;
      onDevice?.(data, { simulated: false });
    } catch {
      onDevice?.(latestDevice, { simulated: false, stale: true });
    }
  }

  async function fetchModels() {
    if (mode !== 'live') return;
    try {
      const resp = await fetch('/api/models', { cache: 'no-store' });
      if (resp.ok) onModels?.(await resp.json());
    } catch { /* ignore */ }
  }

  async function connectLive({ reset = false } = {}) {
    stopLiveStreams();
    simFeed.pause();
    const leavingSimulation = mode === 'simulated';
    if (reset) {
      currentState = { revision: -1, chat_id: '', owner_code: '', turns: [], chats: [], memories: [], timers: [] };
      streamInitial = true;
      onState?.(currentState, { simulated: false, initial: true });
    }
    if (leavingSimulation) {
      currentState = { revision: -1, chat_id: '', turns: [], chats: [], memories: [], timers: [] };
      latestDevice = null;
      onState?.(currentState, { initial: true });
      onDevice?.(null, { stale: true });
    } else {
      currentState = { ...currentState, revision: -1 };
    }
    mode = 'connecting';
    onLink?.({ mode: 'connecting', online: false, beatId: 'live' });

    try {
      const healthResp = await fetch('/api/health', { cache: 'no-store' });
      if (!healthResp.ok) throw new Error('Backend offline');
      const health = await healthResp.json();
      if (health.offline || health.app !== 'moss-local-chat') throw new Error('Backend offline');
    } catch {
      mode = 'offline';
      onLink?.({ mode, online: false, beatId: 'live' });
      onLog?.({ time: Date.now(), source: 'LINK', text: 'MOSS backend unavailable', level: 'warn' });
      reconnectTimer = setTimeout(tryProbeLive, 12000);
      return;
    }

    mode = 'live';
    streamInitial = true;
    onLink?.({ mode: 'live', online: true, beatId: 'live' });
    onLog?.({ time: Date.now(), source: 'LINK', text: `Connected to MOSS backend ${location.host}`, level: 'ok' });

    // Initial state + models + hardware readings
    fetch('/api/state', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((s) => {
        if (mode === 'live' && streamInitial && Number.isFinite(s.revision)) {
          currentState = s;
          streamInitial = false;
          onState?.(s, { simulated: false, initial: true });
        }
      })
      .catch(() => {});

    fetchModels();
    pollDeviceStatus();
    deviceTimer = setInterval(pollDeviceStatus, 5000); // fui-lint-ignore: R15 real 5s hardware telemetry poll; skips when paused/hidden

    // Primary SSE state stream
    eventSource = new EventSource('/api/events?wake=1');
    eventSource.addEventListener('open', () => {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (mode === 'offline') streamInitial = true;
      mode = 'live';
      onLink?.({ mode: 'live', online: true, beatId: 'live' });
      pollDeviceStatus();
    });
    eventSource.addEventListener('state', (ev) => {
      try {
        const next = JSON.parse(ev.data);
        const ownerChanged = Boolean(next.owner_code && currentState.owner_code && next.owner_code !== currentState.owner_code);
        if (streamInitial || ownerChanged || next.revision > currentState.revision) {
          currentState = next;
          const initial = streamInitial || ownerChanged;
          streamInitial = false;
          onState?.(next, { simulated: false, initial });
        }
      } catch (err) {
        onError?.(err.message);
      }
    });
    eventSource.addEventListener('auth-required', () => {
      stopLiveStreams();
      window.dispatchEvent(new CustomEvent('moss-auth-required'));
    });
    eventSource.addEventListener('wake-offer', (ev) => {
      onWakeOffer?.(ev.data);
    });
    eventSource.addEventListener('brain-alert', (ev) => {
      try {
        const alert = JSON.parse(ev.data);
        window.dispatchEvent(new CustomEvent('moss-brain-alert', { detail: alert }));
      } catch {}
    });
    eventSource.addEventListener('error', () => {
      mode = 'offline';
      if (eventSource) {
        try { eventSource.close(); } catch {}
        eventSource = null;
      }
      if (runtimeLogSource) {
        try { runtimeLogSource.close(); } catch {}
        runtimeLogSource = null;
      }
      onDevice?.(latestDevice, { stale: true });
      onLink?.({ mode, online: false, beatId: 'live' });
      onLog?.({ time: Date.now(), source: 'SSE', text: 'Connection interrupted · retrying', level: 'warn' });
      void fetch('/api/auth/status', { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : null))
        .then((auth) => {
          if (auth && auth.enabled && !auth.authorized) {
            stopLiveStreams();
            window.dispatchEvent(new CustomEvent('moss-auth-required'));
          } else if (mode === 'offline' && !reconnectTimer) {
            reconnectTimer = setTimeout(tryProbeLive, 4000);
          }
        })
        .catch(() => {
          if (mode === 'offline' && !reconnectTimer) reconnectTimer = setTimeout(tryProbeLive, 4000);
        });
    });

    // Real-time backend log stream (CS admin only; prevents C-role HTTP 403 noise)
    if (document.body.dataset.authRole === 'CS') {
      runtimeLogSource = new EventSource('/api/runtime-log/stream');
      runtimeLogSource.onmessage = (ev) => {
        try {
          const entry = JSON.parse(ev.data);
          if (typeof entry.text === 'string') {
            onLog?.({
              time: entry.time || Date.now(),
              source: entry.source || 'CORE',
              text: entry.text,
              level: entry.level || (entry.error ? 'warn' : 'info'),
              file: entry.file || null,
            });
          }
        } catch { /* ignore */ }
      };
    }
  }

  async function tryProbeLive() {
    if (mode !== 'offline') return;
    try {
      const r = await fetch('/api/health', { cache: 'no-store' });
      if (r.ok) {
        const h = await r.json();
        if (!h.offline && h.app === 'moss-local-chat') {
          connectLive();
          return;
        }
      }
    } catch { /* still offline */ }
    reconnectTimer = setTimeout(tryProbeLive, 15000);
  }

  async function postAction(verb, body = {}) {
    if (mode === 'simulated') {
      return handleSimulatedAction(verb, body);
    }
    if (mode !== 'live') throw new Error('MOSS 后端未连接。');
    const resp = await fetch(`/api/${verb}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 401) window.dispatchEvent(new CustomEvent('moss-auth-required'));
    if (!resp.ok) throw new Error(data.error || data.detail || `HTTP ${resp.status}`);
    acceptState(data);
    return data;
  }

  function acceptState(data) {
    if (mode === 'live' && Array.isArray(data.turns) && Number.isFinite(data.revision) &&
        ((data.owner_code && currentState.owner_code && data.owner_code !== currentState.owner_code) ||
         data.revision >= currentState.revision)) {
      currentState = data;
      onState?.(data, { simulated:false });
    }
  }

  function handleSimulatedAction(verb, body) {
    const nowIso = new Date().toISOString();
    if (verb === 'message' && body.text) {
      const id = `sim-turn-${Date.now()}`;
      const txt = body.text;
      const isDraft = /草稿|draft/i.test(txt);
      const isDoc = /pdf|word|docx|(?:^|[^图])文档|报告|文件|document/i.test(txt);
      const isImage = /图片|照片|看图|图纸|image|photo/i.test(txt);
      const isVideo = /视频|影片|录像|video|mv/i.test(txt);
      const isMedia = /播放|听歌|点歌|音乐|歌曲|曲库|媒体|music|play\s/i.test(txt);
      const isWeather = /天气|气象|预报|weather/i.test(txt);
      const isHost = /几点|电量|音量|硬件|本机状态|cpu|内存|status|battery/i.test(txt);
      const isContext = /剪贴板|截屏|窗口|ocr|clipboard/i.test(txt);
      const isIntel = /550w|moss|ueg|bsc|联合政府|北京航天|太空电梯|领航员|档案|资料|brief|dossier/i.test(txt);

      const simMediaItems = [
        { media_id: 'sim-media-01', title: '带你去流浪', artist: '刘欢 · 流浪地球原声', kind: 'audio', duration_sec: 264, simulated: true },
        { media_id: 'sim-media-02', title: '冰雨', artist: '刘德华', kind: 'audio', duration_sec: 279, simulated: true },
        { media_id: 'sim-media-03', title: '2002年的第一场雪', artist: '刀郎', kind: 'audio', duration_sec: 234, simulated: true },
      ];

      let turn = {
        id,
        chat_id: currentState.chat_id || 'sim-chat-01',
        created_at: nowIso,
        replied_at: nowIso,
        user: txt,
        status: 'replied',
        cloud_requests: { total: 0, by_provider: {} },
      };

      if (isDraft) {
        turn.status = 'pending';
        turn.preview = txt.replace(/^.*[：:]/, '').trim() || txt;
      } else if (isDoc) {
        const isPdf = /pdf/i.test(txt);
        turn.reply = isPdf
          ? '已调取 PDF 工程简报并在光学台侧翼打开专用文档查看器（支持拖拽或点选本机真实 PDF / Word 文件直接预览）。'
          : '已解析 Word (.docx) 巡检档案并在侧翼打开专用文档查看器（可直接拖入本机 .docx / .pdf 文件查看）。';
        turn.document = {
          format: isPdf ? 'pdf' : 'word',
          title: isPdf ? 'UEG-550W_轨道测控简报.pdf' : '550W_日常巡检核对报告.docx',
          pages: isPdf ? 'PDF · 04 PAGES' : 'DOCX · 3 SECTIONS',
          blocks: [
            { kind: 'heading', text: '一、550W 量子主机运行与测控摘要' },
            { kind: 'paragraph', text: '本机 HoloJarvis 推理链路与光学镜组云台均处于标称区间，六灯状态阵列响应延迟小于 12 ms。支持通过侧翼 FUI 浮窗直接查阅 PDF、Word (.docx)、图片及音视频文件，且默认避让中心 550W 三维机身。' },
            { kind: 'heading', text: '二、关键遥测核对指标' },
            {
              kind: 'table',
              rows: [
                ['监测子系统', '当前读数', '状态'],
                ['处理器负载', `${Math.round(simHw.cpu)} %`, 'NOMINAL'],
                ['可用内存余量', `${simHw.ramAvailGb.toFixed(1)} GiB`, 'NOMINAL'],
                ['电池与供电', `${Math.round(simHw.bat)} % AC`, 'ONLINE'],
              ],
            },
            { kind: 'paragraph', text: '提示：点击右上角「+ 文件」或直接将本机 PDF / Word / 图片 / 音视频拖放到舞台区域，即可立即载入真实文件查看。' },
          ],
        };
      } else if (isImage) {
        turn.reply = '已调出专用图片查看器（支持缩放、90° 旋转、原图打开与多图切换），并自动避开 550W 机身。';
        turn.images = [
          { src: '/vendor/logos/550w.svg', title: '550W 官方标识矢量图', caption: '550W Quantum Computer Vector Plate · 3570×1154' },
          { src: '/vendor/logos/ueg-emblem.svg', title: 'UEG 联合政府纹章', caption: 'United Earth Government Seal · 3000×3500' },
          { src: '/vendor/logos/space-elevator.svg', title: '太空电梯工程标识', caption: 'UEG Space Elevator Program · 3000×1400' },
        ];
      } else if (isVideo) {
        const vidItem = { media_id: 'sim-video-01', url: '/conversation-assets/moss-opening.mp4', title: '550W 启动实机影像', artist: 'UEG · BSC ARCHIVE', kind: 'video', duration_sec: 12 };
        turn.reply = '已打开专用视频播放器（支持倍速播放、展开全幅及声纹同步）。';
        turn.media = { title: '视频影像投影', items: [vidItem], selected: vidItem, autoplay: false };
      } else if (isMedia) {
        const picked = /刀郎/.test(txt) ? simMediaItems[2] : /刘德华|冰雨/.test(txt) ? simMediaItems[1] : simMediaItems[0];
        turn.reply = `已检索到 3 项本地媒体记录，已调出多媒体播控投影：《${picked.title}》— ${picked.artist}。`;
        turn.media = { title: '本机媒体库检索', items: simMediaItems, selected: picked, autoplay: false };
      } else if (isContext) {
        const action = /剪贴板|clipboard/i.test(txt) ? 'clipboard' : /截屏|画面/i.test(txt) ? 'screenshot' : 'screen_text';
        turn.reply = '已打开上下文采集投影，请选择目标窗口或读取剪贴板后发送分析。';
        turn.context_action = { action, prompt: '捕获窗口文字或剪贴板片段，交由 550W 核心核对。' };
      } else if (isWeather) {
        const city = /北京/.test(txt) ? '北京' : /深圳/.test(txt) ? '深圳' : '上海';
        const coords = city === '北京' ? [39.9042, 116.4074] : city === '深圳' ? [22.5431, 114.0579] : [31.2304, 121.4737];
        turn.reply = `${city}当前气象监测：局部多云，气温 11–18 °C，降水概率 20%，风速 14.2 km/h。已同步至气象情报投影。`;
        turn.weather = {
          source: 'Open-Meteo',
          city,
          location_kind: 'city',
          latitude: coords[0],
          longitude: coords[1],
          code: 2,
          condition: '局部多云',
          temperature_min: 11,
          temperature_max: 18,
          precipitation_probability: 20,
          wind_speed: 14.2,
          days: [
            { date: '2026-11-22', code: 1, condition: '大致晴朗', temperature_min: 10, temperature_max: 19, precipitation_probability: 10 },
            { date: '2026-11-23', code: 2, condition: '局部多云', temperature_min: 11, temperature_max: 18, precipitation_probability: 20 },
            { date: '2026-11-24', code: 61, condition: '小雨', temperature_min: 9, temperature_max: 15, precipitation_probability: 65 },
          ],
        };
      } else if (isHost) {
        const clockStr = new Date().toTimeString().slice(0, 8);
        turn.reply = `本机时间 ${clockStr}，处理器负载 ${Math.round(simHw.cpu)}%，可用内存 ${simHw.ramAvailGb.toFixed(1)} GiB，电池电量 ${Math.round(simHw.bat)}%。已推送至本机状态投影。`;
        turn.local_overview = [
          { kind: 'clock', reply: `本机时钟 ${clockStr}` },
          { kind: 'battery', reply: `电量 ${Math.round(simHw.bat)}%（${simHw.ac ? '外接供电' : '电池供电'}）` },
          { kind: 'volume', reply: '系统音量 64%' },
        ];
      } else if (isIntel) {
        turn.reply = '已检索 UEG / 北京航天中心加密数据库，并将关联图文档案推送至光学观察台情报投影窗。';
      } else {
        turn.reply = `已收到指令（模拟演练）：“${txt}”。启动后端 127.0.0.1:18221 后将自动无缝接入 HoloJarvis 核心与 OmniVoice 语音。`;
      }

      simTurns = [...simTurns, turn];
      emitSimSnapshot(new Date());
      onLog?.({ time: Date.now(), source: 'SIM', text: `Directive processed (${id})`, level: 'ok' });
      return currentState;
    }
    if (verb === 'media-select' && body.turn_id) {
      simTurns = simTurns.map((tr) => {
        if (tr.id !== body.turn_id || !tr.media?.items?.length) return tr;
        const picked = tr.media.items.find((x) => x.media_id === body.media_id) || tr.media.items[(body.index || 1) - 1];
        return picked ? { ...tr, media: { ...tr.media, selected: picked } } : tr;
      });
      emitSimSnapshot(new Date());
      return currentState;
    }
    if (verb === 'approve') {
      simTurns = simTurns.map((tr) =>
        tr.id === body.turn_id
          ? { ...tr, status: 'saved', receipt: { filename: `${tr.id}.txt`, text: tr.preview || 'Verified content' }, reply: '草稿已在模拟模式下逐字节核验保存。' }
          : tr,
      );
      emitSimSnapshot(new Date());
      return currentState;
    }
    if (verb === 'cancel') {
      simTurns = simTurns.map((tr) => (tr.id === body.turn_id ? { ...tr, status: 'cancelled' } : tr));
      emitSimSnapshot(new Date());
      return currentState;
    }
    if (verb === 'new-chat') {
      simTurns = [];
      emitSimSnapshot(new Date());
      return currentState;
    }
    if (verb === 'remember' && body.text) {
      currentState.memories = [
        { id: `mem-${Date.now()}`, text: body.text, source: 'panel', created_at: nowIso },
        ...(currentState.memories || []),
      ];
      emitSimSnapshot(new Date());
      return currentState;
    }
    if (verb === 'forget' && body.id) {
      currentState.memories = (currentState.memories || []).filter((m) => m.id !== body.id);
      emitSimSnapshot(new Date());
      return currentState;
    }
    if (verb === 'select-model' && body.id) {
      const names = { deepseek: 'DeepSeek-V3', aliyun: 'Qwen-Plus', kimi: 'Kimi-8K', local: 'Qwen3.5-4B' };
      currentState.model_id = body.id;
      currentState.model = names[body.id] || body.id;
      emitSimSnapshot(new Date());
      return currentState;
    }
    if (verb === 'select-personality' && body.id) {
      const valid = SIM_PERSONALITIES.find((p) => p.id === body.id);
      if (valid) currentState.personality_id = valid.id;
      emitSimSnapshot(new Date());
      return currentState;
    }
    return currentState;
  }

  async function fetchNotes({ q = '', archived = false, before = 0 } = {}) {
    if (mode === 'simulated') {
      return {
        items: [
          { id: 1, text: '核对 550W 三维机身渲染与顶部导轨阻尼参数', archived: false, version: 1 },
          { id: 2, text: '周五前完成 FUI 控制台 1440 / 768 / 375 验收截图', archived: false, version: 1 },
        ].filter((n) => Boolean(n.archived) === archived && (!q || n.text.includes(q))),
        before: 0,
      };
    }
    const qsNotes = new URLSearchParams({ q, archived: archived ? '1' : '0', before: String(before) });
    const r = await fetch(`/api/notes?${qsNotes}`, { cache: 'no-store' });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }

  async function saveNote({ note = null, action = 'edit', text = null } = {}) {
    if (mode === 'simulated') return { item: { id: note?.id || Date.now(), text: text || note?.text, version: 2 } };
    const payload = note ? { version: note.version, action, ...(text === null ? {} : { text }) } : { text };
    const url = note ? `/api/notes/${note.id}` : '/api/notes';
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }

  async function dismissTimer(id) {
    if (mode === 'simulated') {
      currentState.timers = (currentState.timers || []).filter((tm) => tm.id !== id);
      emitSimSnapshot(new Date());
      return { ok: true };
    }
    const r = await fetch('/api/timer-dismiss', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }

  async function fetchLocalWeather(lat, lon) {
    if (mode === 'simulated') throw new Error('Live weather unavailable in simulation');
    const r = await fetch(`/fui/local-weather?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`, { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  async function getRuntimeLogSource() {
    if (mode !== 'live' || (document.body?.dataset?.authRole && document.body.dataset.authRole !== 'CS')) {
      return { runtime_log: mode !== 'live' ? 'simulated.log' : 'restricted (C)', extra_path: '', extra_exists: false };
    }
    const r = await fetch('/api/runtime-log/source', { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  async function submitJevHandoff(payload) {
    if (mode === 'simulated') return { status: 'filled' };
    const r = await fetch('/api/context/jev-handoff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }

  function setPaused(next) {
    paused = Boolean(next);
    if (paused) simFeed.pause();
    else if (mode === 'simulated') simFeed.resume();
    else if (mode === 'live') pollDeviceStatus();
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && mode === 'live' && !paused) pollDeviceStatus();
  });

  return {
    start() {
      const sc = initScenario();
      if (forcedSim) enterSimulated(sc.beat);
      else connectLive();
    },
    reconnect: connectLive,
    forceSimulated(enabled) {
      const sc = initScenario();
      if (enabled) enterSimulated(sc.beat);
      else connectLive();
    },
    getMode: () => mode,
    getState: () => currentState,
    getDevice: () => latestDevice,
    getBeat: () => simBeat,
    goBeat: (id) => initScenario().go(id),
    setPaused,
    isPaused: () => paused,
    postAction,
    acceptState,
    fetchNotes,
    saveNote,
    dismissTimer,
    fetchLocalWeather,
    submitJevHandoff,
    getRuntimeLogSource,
    refreshModels: fetchModels,
  };
}
