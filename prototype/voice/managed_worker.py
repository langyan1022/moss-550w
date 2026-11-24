import os
from pathlib import Path
import runpy
import sys
from importlib.metadata import PackageNotFoundError, version

import pywintypes
import win32api
import win32job
from winerror import ERROR_FILE_NOT_FOUND


def tested_voice_packages():
    try:
        return version("transformers") == "5.15.1" and version("omnivoice") == "0.5.7"
    except PackageNotFoundError:
        return False


if __name__ == "__main__":
    # Windows 虚拟环境的启动器会再创建解释器。让真正运行推理的进程加入所属服务的作业。
    try:
        job = win32job.OpenJobObject(win32job.JOB_OBJECT_ASSIGN_PROCESS, False, os.environ.pop("MOSS_SPEECH_JOB"))
    except pywintypes.error as error:
        if error.winerror != ERROR_FILE_NOT_FOUND:
            raise
        # 回收可能早于 Windows 虚拟环境的解释器启动，此时不再装载模块。
        raise SystemExit(130) from None
    try:
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    finally:
        job.Close()
    if os.environ.get("MOSS_OMNIVOICE_MINIMAL_IMPORTS", "1") == "1" and tested_voice_packages():
        # Transformers 5.15.1 eagerly imports scikit-learn for assisted decoding.
        # OmniVoice uses masked audio-token generation. Disable this optional import in this worker.
        sys.modules["sklearn"] = None
        if os.environ.get("MOSS_OMNIVOICE_AUDIO_ONLY_IMPORTS", "1") == "1":
            # The text and audio tokenizer do not use the image transforms imported by the detection losses.
            sys.modules["torchvision"] = None
    runpy.run_path(str(Path(__file__).with_name("clone_omnivoice.py")), run_name="__main__")
