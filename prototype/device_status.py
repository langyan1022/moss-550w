import asyncio
import csv
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess
from time import monotonic, sleep
import unicodedata


SAMPLE_SECONDS = .20
PANEL_CACHE_SECONDS = 2.0
HARDWARE_CACHE_SECONDS = 4.0
HARDWARE_MAX_AGE = 15.0
GIB = 1 << 30


class MemoryStatus(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                ("total_pagefile", ctypes.c_ulonglong), ("available_pagefile", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                ("available_extended", ctypes.c_ulonglong)]


class PowerStatus(ctypes.Structure):
    _fields_ = [("ac", wintypes.BYTE), ("flags", wintypes.BYTE), ("percent", wintypes.BYTE),
                ("saver", wintypes.BYTE), ("life", wintypes.DWORD), ("full_life", wintypes.DWORD)]


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
kernel.GetSystemTimes.restype = wintypes.BOOL
kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
kernel.GetSystemPowerStatus.argtypes = [ctypes.POINTER(PowerStatus)]
kernel.GetSystemPowerStatus.restype = wintypes.BOOL
kernel.GetTickCount64.argtypes = []
kernel.GetTickCount64.restype = ctypes.c_ulonglong
kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
kernel.GetDriveTypeW.restype = wintypes.UINT


READ = r"(?:(?:查看|看看|看一下|查查|查一下|查询|显示)\s*)?"
CURRENT = r"(?:(?:本机|本地|系统|电脑|这台电脑)(?:上|的)?|(?:当前|现在|目前)的?)*"
PERCENTAGE = r"(?:(?:占用率?|使用率?|负载)(?:(?:是|有|为)?多少)?|(?:占用|用了|用掉|使用)多少)"
SPACE = r"(?:空间|容量|余量|剩余空间|(?:空间)?(?:还)?剩(?:余)?(?:多少|多大)(?:空间)?|还有多少(?:空间)?)"
BATTERY_TIME = r"(?:当前|现在|目前|这会儿)的?"
BATTERY_DEVICE = rf"(?:{BATTERY_TIME})?(?:我的?|这台)?(?:笔记本(?:电脑)?|电脑)(?:上|的)?(?:{BATTERY_TIME})?"
BATTERY_AMOUNT = r"(?:多少|百分之(?:多少|几)|多少百分比)"
BATTERY_DETAIL = rf"(?:还(?:有|剩(?:余)?)?|有|剩(?:余)?|是|为)?{BATTERY_AMOUNT}(?:的?电(?:量)?)?"
BATTERY_QUERY = re.compile(
    rf"{READ}(?:{BATTERY_DEVICE}|我的?(?:{BATTERY_TIME})?|{BATTERY_TIME}|{CURRENT})"
    rf"(?:电池状态|(?:电池)?电量(?:{BATTERY_DETAIL})?|电池{BATTERY_DETAIL}"
    rf"|(?:还)?(?:有|剩(?:余)?)?{BATTERY_AMOUNT}的?电(?:量)?"
    rf"|电(?:还)?(?:有|剩(?:余)?){BATTERY_AMOUNT}"
    r"|有没有在充电|(?:还)?在充电)(?:了)?[啊呀呢吗么]?"
)


def parse_device_status(text):
    phrase = "".join(unicodedata.normalize("NFKC", text).casefold().split()).rstrip("。!?")
    if re.fullmatch(r"(?:(?:怎么|为什么|为何))?(?:我的?|这台)?(?:电脑|机器|系统|本机)(?:现在|这会儿|这几天)?"
                    r"(?:怎么|为什么|为何)?(?:才会|会|突然|总是)?(?:这么|那么|有点|很|好|太)?"
                    r"(?:卡顿|卡|慢)(?:了|得厉害)?", phrase):
        return {"action":"device_status", "metric":"diagnosis"}
    if re.fullmatch(rf"{READ}{CURRENT}(?:设备状态|电脑状态|系统状态|本机状态|运行状态|资源(?:情况|占用))"
                    r"(?:怎么样|如何|情况)?", phrase):
        return {"action":"device_status", "metric":"overview"}
    if re.fullmatch(rf"{READ}{CURRENT}(?:cpu|处理器)(?:现在|当前)?{PERCENTAGE}", phrase):
        return {"action":"device_status", "metric":"cpu"}
    if re.fullmatch(rf"{READ}{CURRENT}(?:内存(?:当前|现在)?(?:{PERCENTAGE}|剩余(?:多少)?|还(?:有|剩)多少|可用量|容量)"
                    r"|(?:还)?(?:有|剩)多少(?:可用)?内存|(?:可用|剩余)内存(?:有多少)?)", phrase):
        return {"action":"device_status", "metric":"memory"}
    if BATTERY_QUERY.fullmatch(phrase):
        return {"action":"device_status", "metric":"battery"}
    if re.fullmatch(rf"{READ}{CURRENT}(?:开机(?:时长|时间|多久了?)|运行多久了?)", phrase):
        return {"action":"device_status", "metric":"uptime"}
    disk = re.fullmatch(rf"{READ}{CURRENT}(?:(?P<drive>[a-z])(?::|盘)|系统盘|磁盘|硬盘)的?{SPACE}", phrase)
    if disk:
        return {"action":"device_status", "metric":"disk", "drive":disk["drive"]}
    return None


def cpu_ticks():
    idle, kernel_time, user = (wintypes.FILETIME() for _ in range(3))
    if not kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel_time), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    return tuple((stamp.dwHighDateTime << 32) + stamp.dwLowDateTime for stamp in (idle, kernel_time, user))


def cpu_percent(before, after):
    idle, system, user = (later - earlier for later, earlier in zip(after, before))
    total = system + user
    return min(100, max(0, round(100 * (total - idle) / total))) if total > 0 else None


def cpu_status():
    before = cpu_ticks()
    sleep(SAMPLE_SECONDS)
    percent = cpu_percent(before, cpu_ticks())
    if percent is None:
        return "CPU 本次未取得有效采样。"
    return f"CPU 在约 {round(SAMPLE_SECONDS * 1000)} 毫秒采样内占用 {percent}%。"


def memory_sample():
    data = MemoryStatus()
    data.length = ctypes.sizeof(data)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(data)):
        raise ctypes.WinError(ctypes.get_last_error())
    return data


def memory_status():
    data = memory_sample()
    return f"内存可用 {data.available / GIB:.1f} GiB，总计 {data.total / GIB:.1f} GiB。"


def power_sample():
    data = PowerStatus()
    if not kernel.GetSystemPowerStatus(ctypes.byref(data)):
        raise ctypes.WinError(ctypes.get_last_error())
    return data


def power_status():
    data = power_sample()
    if data.flags != 255 and data.flags & 128:
        return "Windows 未报告系统电池。"
    supply = {0:"未接电", 1:"已接电"}.get(data.ac, "电源状态未知")
    if data.flags != 255 and data.flags & 8:
        supply += "，正在充电"
    level = f"电量 {data.percent}%" if data.percent <= 100 else "Windows 未报告电量"
    return f"{level}（{supply}）。"


def disk_sample(volume):
    root = volume + "\\"
    kind = kernel.GetDriveTypeW(root)
    return kind, shutil.disk_usage(root) if kind == 3 else None


def disk_status(drive=None):
    volume = drive.upper() + ":" if drive else (os.environ.get("SystemDrive") or "C:").upper()
    label = volume.removesuffix(":")
    kind, usage = disk_sample(volume)
    if kind in (0, 1):
        return f"{label} 盘当前不可用。"
    if kind != 3:
        return f"{label} 盘不是当前电脑的固定磁盘。请指定本机固定盘。"
    total, _, free = usage
    return f"{label} 盘可用 {free / GIB:.1f} GiB，总计 {total / GIB:.1f} GiB。"


def uptime_status():
    seconds = kernel.GetTickCount64() // 1000
    days, left = divmod(seconds, 86400)
    hours, left = divmod(left, 3600)
    day = f"{days} 天 " if days else ""
    return f"本次开机已运行 {day}{hours} 小时 {left // 60} 分钟。"


def snapshot(intent):
    metric = intent["metric"]
    readers = {"cpu":("CPU", cpu_status), "memory":("内存", memory_status), "battery":("电池", power_status),
               "disk":("磁盘", lambda: disk_status(intent.get("drive"))), "uptime":("运行时长", uptime_status)}
    selected = readers.keys() if metric in ("overview", "diagnosis") else (metric,)
    lines = []
    for key in selected:
        label, read = readers[key]
        try:
            lines.append(read())
        except OSError:
            lines.append(f"Windows 暂未返回{label}读数。")
    if metric == "diagnosis":
        lines.append("这是刚才的短时读数。若卡顿时好时坏，可在卡顿发生时再查一次。")
    return "\n".join(lines)


async def device_status(intent):
    return await asyncio.to_thread(snapshot, intent)


def panel_memory():
    data = memory_sample()
    return {"available":data.available, "total":data.total}


def panel_battery():
    data = power_sample()
    present = None if data.flags == 255 else not bool(data.flags & 128)
    return {"present":present, "percent":data.percent if present is not False and data.percent <= 100 else None,
            "ac":{0:False, 1:True}.get(data.ac), "charging":bool(data.flags & 8) if present is True else None}


def panel_disk():
    drive = (os.environ.get("SystemDrive") or "C:").upper()
    if not re.fullmatch(r"[A-Z]:", drive):
        return None
    _, data = disk_sample(drive)
    return {"drive":drive, "free":data.free, "total":data.total} if data is not None else None


def panel_snapshot(previous_ticks):
    readers = {"ticks":cpu_ticks, "memory":panel_memory, "battery":panel_battery, "disk":panel_disk}
    readings = {}
    for key, read in readers.items():
        try:
            readings[key] = read()
        except OSError:
            readings[key] = None
    ticks = readings.pop("ticks")
    readings["cpu_percent"] = cpu_percent(previous_ticks, ticks) if previous_ticks and ticks else None
    readings["sampled_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return readings, ticks


def bounded_reading(value, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 1) if minimum <= number <= maximum else None


def firmware_cpu_temperature():
    import win32com.client

    service = win32com.client.GetObject(r"winmgmts:{impersonationLevel=impersonate}!\\.\root\wmi")
    for device in service.InstancesOf("AsusAtkWmi_WMNB"):
        # DSTS is the read-only device-status query. 0x00110013 reports the firmware CPU temperature.
        params = device.Methods_("DSTS").InParameters.SpawnInstance_()
        params.Properties_("Device_ID").Value = 0x00110013
        result = device.ExecMethod_("DSTS", params)
        raw = int(result.Properties_("device_status").Value)
        if raw & 0x10000 and (temperature := bounded_reading(raw & 0xFFFF, 1, 125)) is not None:
            return {"temperature_c": temperature, "source": "ASUS 固件"}
    return None


def monitor_cpu_temperature():
    import pywintypes
    import win32com.client

    # Use processor sensors published by an existing monitor, if one is running.
    cpu_paths = ("/intelcpu/", "/amdcpu/", "/cpu/")
    priorities = {"CPU Package": 0, "Core (Tdie)": 1, "Core (Tctl/Tdie)": 2}
    for name in ("LibreHardwareMonitor", "OpenHardwareMonitor"):
        try:
            service = win32com.client.GetObject(r"winmgmts:{impersonationLevel=impersonate}!\\.\root" + "\\" + name)
            temperatures = []
            for sensor in service.ExecQuery("SELECT Name,Value,Identifier FROM Sensor WHERE SensorType = 'Temperature'"):
                if sensor.Name in priorities and str(sensor.Identifier).casefold().startswith(cpu_paths):
                    temperature = bounded_reading(sensor.Value, -20, 125)
                    if temperature is not None:
                        temperatures.append((priorities[sensor.Name], -temperature))
            if temperatures:
                _, negative = min(temperatures)
                return {"temperature_c": -negative, "source": name}
        except pywintypes.com_error:
            continue
    return None


def panel_cpu_temperature():
    import pythoncom
    import pywintypes

    pythoncom.CoInitialize()
    try:
        try:
            sensor = firmware_cpu_temperature()
        except (pywintypes.com_error, TypeError, ValueError):
            sensor = None
        return sensor or monitor_cpu_temperature()
    finally:
        pythoncom.CoUninitialize()


def nvidia_readings(output):
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if len(row) != 4:
            continue
        index, name, load, heat = (item.strip() for item in row)
        if not index.isdecimal() or not name:
            continue
        return {"name": name, "percent": bounded_reading(load, 0, 100),
                "temperature_c": bounded_reading(heat, -20, 125), "source": "NVIDIA"}
    return None


def panel_gpu():
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/nvidia-smi.exe"
    vendor = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation/NVSMI/nvidia-smi.exe"
    executable = next((file for file in (system, vendor) if file.is_file()), None)
    if executable is None:
        return None
    try:
        output = subprocess.check_output([str(executable), "--query-gpu=index,name,utilization.gpu,temperature.gpu",
                                          "--format=csv,noheader,nounits"],
                                         encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL,
                                         creationflags=subprocess.CREATE_NO_WINDOW, timeout=1.2)
    except (OSError, subprocess.SubprocessError):
        return None
    return nvidia_readings(output)


def hardware_snapshot():
    return {"cpu": panel_cpu_temperature(), "gpu": panel_gpu(),
            "hardware_sampled_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}


class DeviceStatusCache:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.readings = None
        self.ticks = None
        self.sample_time = 0.0
        self.hardware = None
        self.hardware_time = 0.0
        self.hardware_job = None

    async def refresh_hardware(self):
        try:
            self.hardware = await asyncio.to_thread(hardware_snapshot)
        except (OSError, RuntimeError):
            self.hardware = None
        finally:
            self.hardware_time = monotonic()

    async def read(self):
        async with self.lock:
            age = monotonic() - self.sample_time
            if self.readings is None or age >= PANEL_CACHE_SECONDS:
                # A long gap between visible windows cannot describe current CPU usage.
                previous = self.ticks if age <= 4 * PANEL_CACHE_SECONDS else None
                self.readings, self.ticks = await asyncio.to_thread(panel_snapshot, previous)
                self.sample_time = monotonic()
            hardware_age = monotonic() - self.hardware_time
            due = not self.hardware_time or hardware_age >= HARDWARE_CACHE_SECONDS
            if due and (self.hardware_job is None or self.hardware_job.done()):
                self.hardware_job = asyncio.create_task(self.refresh_hardware())
            recent = self.hardware if hardware_age < HARDWARE_MAX_AGE else None
            return {**self.readings, "cpu": None, "gpu": None, "hardware_sampled_at": None, **(recent or {})}
