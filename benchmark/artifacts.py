"""Shared batch paths and naming for generation, translation, and the viewer."""

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def replace_artifact(source, target):
    # A Windows viewer request can briefly hold the previous snapshot open.
    for attempt in range(6):
        try:
            source.replace(target)
            return
        except PermissionError as error:
            if attempt == 5 or getattr(error, "winerror", None) not in (5, 32):
                raise
            time.sleep(0.05)


def job_image_path(model_id, prompt_index, ratio_index, width, height, seed):
    return f"images/{model_id}/p{prompt_index:04d}-r{ratio_index:02d}-{width}x{height}-s{seed}.png"
