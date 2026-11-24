/* ── Curated Bilingual Tactical Archive Dossiers (图文档案) ── */
export const INTEL_DOSSIERS = [
  {
    id: '550w',
    code: 'INTEL-550W',
    pattern: /550w|moss|莫斯|量子计算|全息|holojarvis|光学镜/i,
    zh: {
      title: '550W 智能量子计算机 · MOSS',
      kicker: 'UEG 核心算力档案 · 绝密',
      summary: '550 系列最新一代便携式量子计算节点，搭载多轴光学红光传感阵列与自主决策框架，负责领航员空间站与地球行星发动机集群的协同计算。',
      specs: [
        ['设备代号', '550W / MOSS'],
        ['隶属机构', 'UEG · 北京航天中心 (BSC)'],
        ['核心构架', '多维量子并行运算舱'],
        ['交互终端', 'TOF 红光镜组 · 六灯遥测阵'],
      ],
      slides: [
        { kind: 'plate-light', src: '/vendor/logos/550w.svg', caption: '图 01 · 550W 机体官方标识（红环光学传感特征）' },
        { kind: 'plate-mask', src: '/vendor/logos/bsc-badge.svg', caption: '图 02 · 北京航天中心 550 系列研发与部署徽章' },
      ],
      followUp: '介绍 UEG 联合政府与北京航天中心',
    },
    en: {
      title: '550W Quantum Computer · MOSS',
      kicker: 'UEG CORE COMPUTE DOSSIER',
      summary: 'Latest-generation 550-series portable quantum node equipped with an articulated red-aperture TOF optical sensor and autonomous coordination core for Earth Engines and Navigator Station.',
      specs: [
        ['Designation', '550W / MOSS'],
        ['Authority', 'UEG · Beijing Space Center'],
        ['Architecture', 'Quantum Parallel Enclosure'],
        ['Optical Array', 'TOF Red Aperture · 6-Lamp Bank'],
      ],
      slides: [
        { kind: 'plate-light', src: '/vendor/logos/550w.svg', caption: 'FIG 01 · Official 550W chassis wordmark' },
        { kind: 'plate-mask', src: '/vendor/logos/bsc-badge.svg', caption: 'FIG 02 · Beijing Space Center engineering seal' },
      ],
      followUp: 'Brief me on UEG and Beijing Space Center',
    },
  },
  {
    id: 'ueg-bsc',
    code: 'INTEL-UEG',
    pattern: /ueg|联合政府|bsc|北京航天中心|航天中心|流浪地球/i,
    zh: {
      title: '地球联合政府 (UEG) · 北京航天中心',
      kicker: '指挥体系与地面测控档案',
      summary: '地球联合政府统一统筹移山计划与流浪地球工程。北京航天中心（BSC）承担 550 系列量子计算机核心研发、地面测控链路与行星发动机协调调度。',
      specs: [
        ['最高机构', 'United Earth Government (UEG)'],
        ['测控中心', '北京航天中心 · BSC'],
        ['基准坐标', '39.9042°N 116.4074°E'],
        ['主控链路', '550W 量子中继 · 实时同步'],
      ],
      slides: [
        { kind: 'plate-mask', src: '/vendor/logos/ueg-emblem.svg', caption: '图 01 · 地球联合政府（UEG）官方纹章' },
        { kind: 'plate-light', src: '/vendor/logos/bsc-wordmark.svg', caption: '图 02 · 北京航天中心（BSC）标准标识' },
      ],
      followUp: '调取太空电梯与领航员空间站档案',
    },
    en: {
      title: 'United Earth Government · BSC',
      kicker: 'COMMAND & CONTROL DOSSIER',
      summary: 'The United Earth Government coordinates Project Moving Mountain and the Wandering Earth initiative. Beijing Space Center (BSC) leads 550-series quantum engineering and orbital telemetry.',
      specs: [
        ['Authority', 'United Earth Government (UEG)'],
        ['Facility', 'Beijing Space Center (BSC)'],
        ['Ref Fix', '39.9042°N 116.4074°E'],
        ['Core Link', '550W Quantum Relay · Live'],
      ],
      slides: [
        { kind: 'plate-mask', src: '/vendor/logos/ueg-emblem.svg', caption: 'FIG 01 · Official UEG emblem' },
        { kind: 'plate-light', src: '/vendor/logos/bsc-wordmark.svg', caption: 'FIG 02 · Beijing Space Center wordmark' },
      ],
      followUp: 'Open Space Elevator and Navigator Station dossier',
    },
  },
  {
    id: 'space-elevator',
    code: 'INTEL-SE01',
    pattern: /太空电梯|移山计划|加蓬|轿厢|同步轨道/i,
    zh: {
      title: '太空电梯 · 移山计划先导工程',
      kicker: 'UEG 轨道运输基础设施档案',
      summary: '连接地球赤道基座与同步轨道方舟空间站的重型垂直运载通道，采用多级磁悬浮轿厢与反推减速模块，承担行星发动机建造阶段的轨道物资输送。',
      specs: [
        ['工程编号', 'UEG-SE-01'],
        ['轨道高度', '90,000 km 同步轨道'],
        ['推进方式', '磁悬浮导轨 + 发动机助推'],
        ['测控接管', '550C / 550W 冗余链路'],
      ],
      slides: [
        { kind: 'plate-mask', src: '/vendor/logos/space-elevator.svg', caption: '图 01 · 太空电梯系统工程徽标' },
        { kind: 'plate-dark', src: '/vendor/logos/navigator-seal-dark.svg', caption: '图 02 · 领航员国际空间站联合任务纹章' },
      ],
      followUp: '查看 550W 量子计算机技术档案',
    },
    en: {
      title: 'Space Elevator · Moving Mountain',
      kicker: 'ORBITAL LIFT INFRASTRUCTURE',
      summary: 'Heavy vertical transport corridor linking equatorial surface bases to geostationary orbital stations, delivering structural payloads via maglev carriages and booster stages.',
      specs: [
        ['Project ID', 'UEG-SE-01'],
        ['Apex', '90,000 km Geostationary'],
        ['Propulsion', 'Maglev Rail + Booster Burn'],
        ['Control', '550C / 550W Redundant Link'],
      ],
      slides: [
        { kind: 'plate-mask', src: '/vendor/logos/space-elevator.svg', caption: 'FIG 01 · Space Elevator program insignia' },
        { kind: 'plate-dark', src: '/vendor/logos/navigator-seal-dark.svg', caption: 'FIG 02 · Navigator International Space Station seal' },
      ],
      followUp: 'Inspect 550W Quantum Computer technical dossier',
    },
  },
  {
    id: 'navigator',
    code: 'INTEL-NAV',
    pattern: /领航员|空间站|火种|刘培强|木星/i,
    zh: {
      title: '领航员国际空间站 · NAVIGATOR',
      kicker: '深空领航与通讯中继档案',
      summary: '领航员号部署于地球前进航线前方，配备大功率深空预警雷达、休眠舱与 550W 核心控制主机，负责航线校准、通讯中继与应急火种保存。',
      specs: [
        ['设施编号', 'NAV-ISS-01'],
        ['驻站核心', '550W (MOSS)'],
        ['主责任务', '深空预警 · 航线领航'],
        ['通讯协议', 'UEG 量子加密广播'],
      ],
      slides: [
        { kind: 'plate-dark', src: '/vendor/logos/navigator-seal-dark.svg', caption: '图 01 · 领航员国际空间站任务纹章' },
        { kind: 'plate-mask', src: '/vendor/logos/ueg-emblem.svg', caption: '图 02 · 地球联合政府授权指挥徽章' },
      ],
      followUp: '查看 550W 量子计算机技术档案',
    },
    en: {
      title: 'Navigator Space Station',
      kicker: 'DEEP-SPACE RELAY & GUIDANCE',
      summary: 'Positioned ahead of Earth’s trajectory, Navigator Station houses deep-space radar, cryo-bays, and the 550W core host for navigation calibration and emergency seed storage.',
      specs: [
        ['Station ID', 'NAV-ISS-01'],
        ['Onboard Core', '550W (MOSS)'],
        ['Mission', 'Trajectory Guidance & Relay'],
        ['Protocol', 'UEG Quantum Encrypted Link'],
      ],
      slides: [
        { kind: 'plate-dark', src: '/vendor/logos/navigator-seal-dark.svg', caption: 'FIG 01 · Navigator Station official seal' },
        { kind: 'plate-mask', src: '/vendor/logos/ueg-emblem.svg', caption: 'FIG 02 · United Earth Government command seal' },
      ],
      followUp: 'Inspect 550W Quantum Computer technical dossier',
    },
  },
];

export function matchIntelDossier(turn) {
  if (!turn) return null;
  if (turn.lore || turn.dossier) return { custom: turn.lore || turn.dossier };
  const haystack = `${turn.user || ''}\n${turn.reply || ''}`;
  for (const item of INTEL_DOSSIERS) {
    if (item.pattern.test(haystack)) return { dossier: item };
  }
  return null;
}
