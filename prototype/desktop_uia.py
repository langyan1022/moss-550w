"""MOSS Windows UI Automation (IUIAutomation via comtypes) + Visual SoM Fallback Advisor.

Provides fast (~15-40ms), zero-VRAM structural introspection of the active Windows foreground
window so MOSS can locate buttons, text fields, tabs, and menus without relying exclusively on
raw pixel OCR, while explicitly flagging Electron/custom-rendered windows for Visual SoM fallback.
"""

from __future__ import annotations

import asyncio
from collections import deque
import ctypes
from typing import Any

import comtypes
import comtypes.client
import psutil
import pythoncom
import win32process


_CONTROL_TYPE_NAMES: dict[int, str] = {
    50000: "Button",
    50001: "Calendar",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50006: "Image",
    50007: "ListItem",
    50008: "List",
    50009: "Menu",
    50010: "MenuBar",
    50011: "MenuItem",
    50012: "ProgressBar",
    50013: "RadioButton",
    50014: "ScrollBar",
    50015: "Slider",
    50016: "Spinner",
    50017: "StatusBar",
    50018: "Tab",
    50019: "TabItem",
    50020: "Text",
    50021: "ToolBar",
    50022: "ToolTip",
    50023: "Tree",
    50024: "TreeItem",
    50025: "Custom",
    50026: "Group",
    50027: "Thumb",
    50028: "DataGrid",
    50029: "DataItem",
    50030: "Document",
    50031: "SplitButton",
    50032: "Window",
    50033: "Pane",
    50034: "Header",
    50035: "HeaderItem",
    50036: "Table",
    50037: "TitleBar",
    50038: "Separator",
}

_INTERACTIVE_TYPES = frozenset({
    50000, 50002, 50003, 50004, 50005, 50007, 50011, 50013, 50015, 50019, 50024, 50030, 50031,
})

_UIA_MODULE = None


def _get_uia_instance():
    global _UIA_MODULE
    if _UIA_MODULE is None:
        _UIA_MODULE = comtypes.client.GetModule("UIAutomationCore.dll")
    uia = comtypes.client.CreateObject(
        _UIA_MODULE.CUIAutomation._reg_clsid_,
        interface=_UIA_MODULE.IUIAutomation,
    )
    return uia


def _safe_rect(el) -> list[int] | None:
    try:
        rc = el.CurrentBoundingRectangle
        w = int(rc.right - rc.left)
        h = int(rc.bottom - rc.top)
        if w <= 0 or h <= 0:
            return None
        return [int(rc.left), int(rc.top), int(rc.right), int(rc.bottom)]
    except Exception:
        return None


def inspect_foreground_uia_sync(*, max_elements: int = 36, max_depth: int = 5) -> dict[str, Any]:
    """Synchronously capture the Windows foreground window UI Automation accessibility tree."""
    pythoncom.CoInitialize()
    try:
        uia = _get_uia_instance()
        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        fallback_root = False
        pid = 0
        proc_name = ""
        if hwnd:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                proc_name = psutil.Process(int(pid)).name()
            except Exception:
                proc_name = ""
            root = uia.ElementFromHandle(hwnd)
            title = str(getattr(root, "CurrentName", "") or "").strip()[:120]
            eff_max_depth = max(1, int(max_depth))
        else:
            fallback_root = True
            proc_name = "Desktop"
            root = uia.GetRootElement()
            title = "Desktop Root (Visible Windows)"
            eff_max_depth = min(2, max(1, int(max_depth)))

        win_rect = _safe_rect(root) or [0, 0, 0, 0]
        walker = uia.ControlViewWalker

        elements: list[dict[str, Any]] = []
        interactive_count = 0
        queue = deque([(root, 0)])
        seen_nodes = 0
        max_visit = max(80, int(max_elements) * 6)

        while queue and len(elements) < max_elements and seen_nodes < max_visit:
            node, depth = queue.popleft()
            seen_nodes += 1
            if depth > 0:
                try:
                    ctype = int(node.CurrentControlType)
                    name = str(node.CurrentName or "").strip()
                    auto_id = str(node.CurrentAutomationId or "").strip()
                    offscreen = bool(node.CurrentIsOffscreen)
                    rect = _safe_rect(node)
                    if not offscreen and rect and (name or auto_id or ctype in _INTERACTIVE_TYPES):
                        is_inter = ctype in _INTERACTIVE_TYPES
                        if is_inter:
                            interactive_count += 1
                        if name or auto_id:
                            elements.append({
                                "id": len(elements) + 1,
                                "control_type": _CONTROL_TYPE_NAMES.get(ctype, f"Type({ctype})"),
                                "name": name[:80],
                                "automation_id": auto_id[:48],
                                "interactive": is_inter,
                                "rect": rect,
                                "center": [(rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2],
                            })
                except Exception:
                    pass

            if depth >= eff_max_depth:
                continue
            try:
                child = walker.GetFirstChildElement(node)
                sib_guard = 0
                while child is not None and sib_guard < 40:
                    queue.append((child, depth + 1))
                    child = walker.GetNextSiblingElement(child)
                    sib_guard += 1
            except Exception:
                continue

        win_w = max(0, win_rect[2] - win_rect[0])
        win_h = max(0, win_rect[3] - win_rect[1])
        som_recommended = bool(interactive_count <= 2 and win_w >= 300 and win_h >= 200)

        header = (
            f"桌面顶层窗口（回退扫描）：{title}（尺寸 {win_w}x{win_h}）"
            if fallback_root
            else f"前景窗口：{title or '未知标题'}（进程 {proc_name or pid}，尺寸 {win_w}x{win_h}）"
        )
        lines = [header]
        if elements:
            lines.append(f"UIA 可见控件（共 {len(elements)} 项，交互控件 {interactive_count} 项）：")
            for el in elements[:20]:
                aid = f" #{el['automation_id']}" if el["automation_id"] else ""
                lines.append(
                    f"- [U#{el['id']} {el['control_type']}{aid}] \"{el['name']}\" @ ({el['center'][0]}, {el['center'][1]})"
                )
        else:
            lines.append("未能从 UIA 树提取出命名控件。")
        if som_recommended:
            lines.append("提示：该窗口交互节点较少（可能为自绘/WebGL/Electron 界面），建议结合视觉 SoM 截图识别兜底。")

        return {
            "ok": True,
            "hwnd": hwnd,
            "pid": int(pid),
            "process": proc_name,
            "title": title,
            "rect": win_rect,
            "fallback_root": fallback_root,
            "interactive_count": interactive_count,
            "som_fallback_recommended": som_recommended,
            "elements": elements,
            "summary_text": "\n".join(lines),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc) or "Windows UIA 采集失败。",
            "elements": [],
            "som_fallback_recommended": True,
        }
    finally:
        pythoncom.CoUninitialize()


async def inspect_foreground_uia(*, max_elements: int = 36, max_depth: int = 5) -> dict[str, Any]:
    return await asyncio.to_thread(inspect_foreground_uia_sync, max_elements=max_elements, max_depth=max_depth)
