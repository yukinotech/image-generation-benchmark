"""Translate benchmark prompt snapshots independently of the benchmark runner."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from tempfile import NamedTemporaryFile

if __package__:
    from .artifacts import replace_artifact
else:
    from artifacts import replace_artifact

TRANSLATIONS_FILE = "prompt_translations.json"
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")
LATIN = re.compile(r"[A-Za-z]")
Translator = Callable[[str], str]


def read_mapping(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"需要 JSON 对象：{path}")
    return data


def write_translations(path: Path, translations: dict[str, str]) -> None:
    temp_path = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                dir=path.parent, prefix=f".{path.name}.",
                                suffix=".tmp", delete=False) as file:
            temp_path = Path(file.name)
            json.dump(translations, file, ensure_ascii=False, indent=2)
            file.write("\n")
        replace_artifact(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def translate_run(run_dir: Path, translate: Translator, *, force: bool = False) -> Path:
    """Generate the viewer's translation file using an injectable text translator."""
    run_dir = Path(run_dir)
    manifest = read_mapping(run_dir / "manifest.json")
    prompts = manifest.get("prompts")
    if (not isinstance(prompts, dict) or not prompts
            or any(not isinstance(text, str) or not text.strip() for text in prompts.values())):
        raise ValueError(f"批次 manifest 缺少有效的 prompts：{run_dir}")

    output = run_dir / TRANSLATIONS_FILE
    existing = read_mapping(output) if output.is_file() and not force else {}
    translations = {}
    for name, text in prompts.items():
        previous = existing.get(name)
        if HAN.search(text) and not LATIN.search(text):
            translated = text
        elif isinstance(previous, str) and HAN.search(previous):
            translated = previous
        else:
            try:
                translated = translate(text)
            except (OSError, ValueError, RuntimeError) as error:
                raise RuntimeError(f"{name} 翻译失败：{error}") from error
            if not isinstance(translated, str) or not HAN.search(translated):
                raise ValueError(f"{name} 的翻译结果缺少中文")
        translations[name] = translated

    if not output.is_file() or force or translations != existing:
        write_translations(output, translations)
    return output
