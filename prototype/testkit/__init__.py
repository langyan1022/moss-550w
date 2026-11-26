"""Shared MOSS test utilities safe against Windows Hyper-V / WinNAT excluded TCP port ranges."""
from __future__ import annotations

from contextlib import closing
import socket

from browser_lab import BrowserServer, ROOT, confirm_muted_edge, open_browser

WINDOWS_EXCLUDED_PORT_RANGE = range(18222, 18322)


def safe_free_port(host: str = "127.0.0.1") -> int:
    """Bind an OS-assigned ephemeral TCP port, skipping the Windows excluded range (18222-18321)."""
    for _ in range(32):
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            sock.bind((host, 0))
            port = int(sock.getsockname()[1])
            if port not in WINDOWS_EXCLUDED_PORT_RANGE:
                return port
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


__all__ = [
    "BrowserServer",
    "ROOT",
    "WINDOWS_EXCLUDED_PORT_RANGE",
    "confirm_muted_edge",
    "open_browser",
    "safe_free_port",
]
