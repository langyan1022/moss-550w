"""Cross-platform local secret encryption for MOSS 550W.

Uses Windows DPAPI (``win32crypt.CryptProtectData`` / ``CryptUnprotectData``) on Windows
for OS-bound credential protection, and transparently falls back to ``cryptography.fernet``
using an entropy-bound per-machine key file (chmod 0600 on POSIX) on non-Windows hosts
or when portable sealing is requested.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from cryptography.fernet import Fernet, InvalidToken

try:
    import pywintypes  # type: ignore[import-untyped]
    import win32crypt  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    pywintypes = None
    win32crypt = None

_PORTABLE_PREFIX = "fernet1:"


class SecretStoreError(RuntimeError):
    """Raised when a secret cannot be sealed or unsealed by the local store."""


def app_config_dir() -> Path:
    """Return per-user MOSS configuration directory across Windows, macOS, and Linux."""
    override = os.environ.get("MOSS_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "MOSS-550W"
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg_cfg:
        return Path(xdg_cfg) / "MOSS-550W"
    return Path.home() / ".config" / "MOSS-550W"


def _load_or_create_machine_key(key_dir: Path | None = None) -> bytes:
    target_dir = Path(key_dir) if key_dir else app_config_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    key_path = target_dir / ".moss-local.key"
    if key_path.is_file():
        raw = key_path.read_bytes().strip()
        if len(raw) >= 32:
            return raw
    secret_bytes = base64.urlsafe_b64encode(os.urandom(32))
    staged = None
    try:
        with NamedTemporaryFile(mode="wb", dir=target_dir, suffix=".key", delete=False) as fh:
            staged = Path(fh.name)
            fh.write(secret_bytes + b"\n")
        if os.name != "nt":
            os.chmod(staged, 0o600)
        os.replace(staged, key_path)
    finally:
        if staged and staged.exists():
            staged.unlink(missing_ok=True)
    return secret_bytes


def _derive_fernet(entropy: str, key_dir: Path | None = None) -> Fernet:
    master = _load_or_create_machine_key(key_dir)
    digest = hashlib.sha256(master + b"\0" + entropy.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def protect_secret(
    plain: str,
    *,
    entropy: str = "",
    description: str = "MOSS",
    flags: int = 1,
    key_dir: Path | None = None,
    prefer_portable: bool = False,
) -> str:
    """Seal plaintext secret into an encrypted ASCII representation."""
    if not plain:
        return ""
    raw = plain.encode("utf-8")
    if win32crypt is not None and not prefer_portable:
        try:
            blob = win32crypt.CryptProtectData(
                raw,
                description,
                entropy.encode("utf-8"),
                None,
                None,
                flags,
            )
            return base64.b64encode(blob).decode("ascii")
        except Exception as exc:
            raise SecretStoreError("无法使用 Windows DPAPI 加密保存凭据。") from exc

    try:
        token = _derive_fernet(entropy, key_dir=key_dir).encrypt(raw)
        return _PORTABLE_PREFIX + token.decode("ascii")
    except Exception as exc:
        raise SecretStoreError("无法使用本地密钥加密保存凭据。") from exc


def unprotect_secret(
    cipher: str,
    *,
    entropy: str = "",
    flags: int = 1,
    key_dir: Path | None = None,
    raise_on_error: bool = False,
) -> str:
    """Unseal an encrypted ASCII representation back to plaintext."""
    text = str(cipher or "").strip()
    if not text:
        return ""
    if text.startswith(_PORTABLE_PREFIX):
        try:
            raw_tok = text[len(_PORTABLE_PREFIX):].encode("ascii")
            return _derive_fernet(entropy, key_dir=key_dir).decrypt(raw_tok).decode("utf-8")
        except (InvalidToken, ValueError, OSError) as exc:
            if raise_on_error:
                raise SecretStoreError("无法解密本机凭据。") from exc
            return ""

    if win32crypt is None:
        if raise_on_error:
            raise SecretStoreError("当前环境不支持解密 legacy DPAPI 凭据。")
        return ""
    try:
        blob = base64.b64decode(text, validate=True)
        _, plain_bytes = win32crypt.CryptUnprotectData(
            blob,
            entropy.encode("utf-8"),
            None,
            None,
            flags,
        )
        return plain_bytes.decode("utf-8")
    except Exception as exc:
        if raise_on_error:
            raise SecretStoreError("此 Windows 用户无法读取已保存的凭据。") from exc
        return ""
