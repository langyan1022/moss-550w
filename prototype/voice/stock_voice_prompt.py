"""Load registered tensors for the stock voice only when the encoder matches."""

from hashlib import sha256
from importlib.util import find_spec
import json
import os
from pathlib import Path

if __package__:
    from .weight_snapshot import REGISTRATION, checksum, identity, runtime_versions
else:
    from weight_snapshot import REGISTRATION, checksum, identity, runtime_versions


SEEDS = Path(__file__).resolve().with_name("reference-seeds")


def registered_prompt(model, style, transcript, reference_audio, encoder, cuda_name=None):
    if os.environ.get("MOSS_OMNIVOICE_REGISTERED_REFERENCE", "1") != "1":
        return None
    if style not in {"neutral", "warm"} or encoder not in {"cpu", "cuda"}:
        return None
    try:
        seed = json.loads((SEEDS / "registration.json").read_text(encoding="utf-8"))
        stock = seed["styles"][style]
        if (seed["format"] != 1 or transcript != stock["transcript"] or
                sha256(reference_audio).hexdigest() != stock["wav"] or encoder not in stock):
            return None
        if encoder == "cuda" and cuda_name != seed["cuda_name"]:
            return None

        release = json.loads(REGISTRATION.read_text(encoding="utf-8"))
        record = release["record"]
        if (release["sha256"] != seed["model_registration_sha256"] or
                checksum(record) != release["sha256"] or
                record["runtime"] != runtime_versions() or
                record["source"] != identity(model)):
            return None

        # OmniVoice is installed editable here. Check the loaded package source as well as its version.
        package = Path(find_spec("omnivoice").origin).parent
        for name, digest in seed["omnivoice_sources"].items():
            if sha256((package / name).read_bytes()).hexdigest() != digest:
                return None
        path = SEEDS / f"{style}-{encoder}.pt"
        return path if sha256(path.read_bytes()).hexdigest() == stock[encoder] else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
        return None
