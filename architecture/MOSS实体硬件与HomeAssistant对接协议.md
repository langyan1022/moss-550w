# MOSS 实体硬件（550W 桌面体）与 Home Assistant 预留对接协议

> 版本：`moss-hw-v1` · 日期：2026-11-22  
> 适用对象：后续基于 **ESP32-S3 / Raspberry Pi / Arduino + 音箱 + 红眼 LED 环 + 机箱小屏 + 摄像头 + 双轴舵机云台** 打造的桌面实体 MOSS，以及本机/局域网 **Home Assistant** 智能家居中枢。

---

## 一、实体 MOSS 硬件协议（`moss-hw-v1`）

本机后端（[iot_bridge.py](../prototype/iot_bridge.py) 与 [serve.py](../prototype/serve.py)）已经原生开放实时下行控制流与上行遥测接口，后续做硬件外壳/主控板时无需重写 MOSS 后端，直接接入即可：

| 接口 | 方向 | 作用 |
| --- | --- | --- |
| `GET /api/hardware/moss/stream` | MOSS → 实体硬件（SSE 推送） | 实时下发灯光、双轴舵机、机箱小屏、音箱与镜头同步帧 |
| `POST /api/hardware/moss/command` | 控制台 / 大脑 → 实体硬件 | 下发灯态、舵机角度/动作预设（点头/摇头/注视/巡航/回中）、小屏文字 |
| `POST /api/hardware/moss/telemetry` | 实体硬件 → MOSS | 上报毫米波人体存在、距离、环境光、机箱温度与实体按键（唤醒/静音/免打扰） |
| `GET /api/runtime-log/stream` | MOSS → 实体小屏（SSE） | 可直接让实体机箱 H50 IPS/OLED 小屏实时刷屏显示后台日志 |
| `POST /api/context/vision-to-sag` | 实体镜头 → MOSS SAG | 上传实体摄像头截取的 PNG，自动识图提取关键事实并入库 SQLite-SAG |

### 1. 下行控制帧 JSON 规范（`event: hw-frame`）

```json
{
  "protocol": "moss-hw-v1",
  "seq": 12,
  "mode": "listening",
  "led": {
    "rgb": [255, 52, 36],
    "brightness": 220,
    "pattern": "pulse",
    "pulse_hz": 1.8
  },
  "servo": {
    "yaw_deg": 12.0,
    "pitch_deg": -5.0,
    "speed": 0.6,
    "gesture": "look_user"
  },
  "screen": {
    "headline": "MOSS 550W",
    "line1": "CPU 14% · GPU 58°C",
    "line2": "LISTENING",
    "level": "ok"
  },
  "speaker": {
    "active": false,
    "rms": 0.0,
    "volume_hint": 75
  },
  "camera": {
    "active": true,
    "track_face": true
  },
  "updated_at": "2026-11-22T15:40:00+00:00"
}
```

- **五大实体子系统映射建议**：
  1. **红眼灯光（LED）**：WS2812B 环形灯 / 单颗高亮红光 LED（PWM 调光），直接读取 `led.rgb / brightness / pattern / pulse_hz`（`standby` 青蓝呼吸、`listening/speaking` 经典红眼、`thinking` 琥珀扫描、`alert` 红色频闪、`dnd` 微光常亮）。
  2. **双轴舵机云台（Servo）**：两颗 MG996R / 飞特串行总线舵机（水平 `yaw_deg ∈ [-60, +60]`，俯仰 `pitch_deg ∈ [-30, +30]`），直接响应 `gesture` 预设动画（`nod` 点头、`shake` 摇头、`look_user` 面向操作员、`patrol` 慢速巡航、`reset` 回中）。
  3. **机箱小屏幕（H50 Screen）**：1.54"–2.8" SPI IPS / OLED 屏，消费 `screen` 字段或直接订阅 `/api/runtime-log/stream` 滚动刷屏。
  4. **音箱与麦克风（Speaker / Mic）**：树莓派/USB 声卡直连主机输出，或通过 I2S 功放（MAX98357A）+ 全向麦阵列对接全双工语音通道。
  5. **光学镜头（Camera）**：UVC USB 摄像头（直接被本机浏览器 `CAM-IN` 接入并支持一键「识图入库 SAG」），或 ESP32-CAM 拍照 POST 至 `/api/context/vision-to-sag`。

### 2. 上行遥测上报规范（`POST /api/hardware/moss/telemetry`）

```json
{
  "person_present": true,
  "distance_cm": 72,
  "ambient_lux": 180,
  "chassis_temp_c": 36.4,
  "last_button": "wake"
}
```
上报 `last_button: "wake"` 时，MOSS 会立刻触发自主脉搏提醒并将实体灯态切至 `listening`、舵机切至 `look_user`。

---

## 二、Home Assistant 智能家居对接规范

- **入口**：点击顶部「**大脑**」→「**家居与实体**」页签，或调用 `GET/POST /api/iot/config`、`GET /api/ha/entities`、`POST /api/ha/service`。
- **两档安全模式**：
  - **`dry_run`（默认演练模式）**：无需配置令牌即可测试「把客厅主灯关了」「看看书房温湿度」「把书房空调调到 24 度」全链路。
  - **`live`（实机模式）**：填入本机/局域网 HA 地址（如 `http://homeassistant.local:8123`）与 Home Assistant 个人长效访问令牌（Long-Lived Access Token），勾选「允许实机控制」后立即真实控制家居设备。
- **白名单安全闸门**：默认仅放行 `light / switch / climate / scene / script / fan / cover / media_player / sensor / binary_sensor / input_boolean`，**默认拦截门锁（`lock`）与安防撤防（`alarm_control_panel`）**；令牌只落盘本机 SQLite，界面与接口永不明文回显完整令牌。
