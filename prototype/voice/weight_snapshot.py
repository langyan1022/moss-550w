import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path


REGISTRATION = Path(__file__).with_name("gpu-weight-registration.json")
CONFIGS = ("config.json", "tokenizer_config.json", "tokenizer.json", "chat_template.jinja")
CODEC_FILES = ("audio_tokenizer/config.json", "audio_tokenizer/preprocessor_config.json", "audio_tokenizer/model.safetensors")


def identity(model):
    files = ("model.safetensors", *CONFIGS, *CODEC_FILES)
    stamps = {}
    for name in files:
        info = (model / name).stat()
        stamps[name] = [info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino]
    return {"path": str(model.resolve()), "files": stamps}


def runtime_versions():
    return {p: version(p) for p in ("torch", "transformers", "omnivoice", "safetensors")}


def checksum(record):
    packed = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(packed).hexdigest()


def select_weights(original, device, dtype, registration=None):
    original = original.resolve()
    if device != "cuda" or dtype != "float16":
        return original
    if registration is None:
        registration = Path(os.environ.get("MOSS_OMNIVOICE_REGISTRATION", REGISTRATION))
    try:
        # 发布登记时校验全部权重。冷启动只读小清单，并检查文件身份、大小、最后写入时间。
        if registration.stat().st_size > 32768:
            return original
        registration_data = json.loads(registration.read_text(encoding="utf-8"))
        record = registration_data["record"]
        if (record["format"] != 1 or registration_data["sha256"] != checksum(record)
                or record["source"] != identity(original) or record["runtime"] != runtime_versions()):
            return original
        candidate = Path(record["fp16"]["path"])
        if (not candidate.is_absolute() or candidate.name != original.name or candidate == original
                or record["fp16"] != identity(candidate)
                or (candidate / "audio_tokenizer").resolve() != (original / "audio_tokenizer").resolve()):
            return original
        return candidate
    except (OSError, KeyError, ValueError, TypeError, RuntimeError, ImportError):
        return original
