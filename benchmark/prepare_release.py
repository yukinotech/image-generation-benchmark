"""Prepare and audit a local checkout of the static benchmark site."""

import argparse
import getpass
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))  # The portable Python ._pth isolates script directories.
from artifacts import RUNS, read_json
from frontend.serve import RUN_NAME, STATIC, export_site
from run import benchmark_lock, image_complete, plan_rows

MANAGED = ("benchmark", "docs")
SOURCE_FILES = (
    "README.md", "artifacts.py", "config.json", "minimax_ab.cmd", "minimax_ab.py",
    "prepare_release.cmd", "prepare_release.py", "prompt_translation.py", "run.cmd",
    "run.py", "translate_prompts.cmd", "translate_prompts.py", "workflows.py",
    "frontend/app.js", "frontend/index.html", "frontend/run.cmd", "frontend/serve.py",
    "frontend/style.css", "prompts/README.md", "prompts/size_rules.json",
)
PUBLIC_IMAGE = re.compile(r"images/[a-f0-9]{20}/[\w-]+\.png", re.ASCII)
PUBLIC_JSON = re.compile(r"metadata/[a-f0-9]{20}(?:/[\w-]+)?\.json", re.ASCII)


def files_in(directory):
    return {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], encoding="utf-8")


def verify_checkout(repo):
    if repo == ROOT or ROOT.is_relative_to(repo) or repo.is_relative_to(ROOT):
        raise ValueError("请使用独立的 benchmark 发布仓库。")
    if Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve() != repo:
        raise ValueError("--repo 必须指向发布仓库的根目录。")
    if git(repo, "status", "--porcelain", "--", *MANAGED).strip():
        raise ValueError("发布目录有未提交修改，请先审阅并处理。")
    for name in MANAGED:
        directory = repo / name
        if not directory.is_dir() or directory.resolve() != directory:
            raise ValueError(f"发布仓库缺少正常目录：{name}")
        tracked = set(git(repo, "ls-files", "-z", "--", name).rstrip("\0").split("\0"))
        actual = {f"{name}/{relative}" for relative in files_in(directory)}
        if actual != tracked:
            raise ValueError(f"{name} 包含未追踪或被忽略的文件，请先审阅。")
        for entry in directory.rglob("*"):
            if entry.is_symlink() or entry.resolve() != entry:
                raise ValueError(f"{name} 包含链接目录或文件，请先审阅。")


def verify_batch(name):
    if not RUN_NAME.fullmatch(name):
        raise ValueError("批次名称无效。")
    directory = RUNS / name
    manifest = read_json(directory / "manifest.json")
    prompts = manifest["prompts"]
    if name.startswith("minimax-ab-") or list(prompts) == ["smoke.txt"]:
        raise ValueError("请选择需要发布的正式批次。")
    prompts = {key: prompts[key] for key in manifest.get("prompt_order", prompts)}
    sizes = manifest["sizes"]
    sizes = {key: sizes[key] for key in manifest.get("ratio_order", sizes)}
    rows = plan_rows(manifest["models"], prompts, sizes, manifest["seeds"],
                     manifest.get("size_rules", {}), manifest.get("model_sizes"))
    if not rows:
        raise ValueError(f"{name} 没有可发布的生成任务。")
    for row in rows:
        state_path = (directory / row["image"]).with_suffix(".json")
        if not state_path.is_file() or not image_complete(directory, row, read_json(state_path)):
            raise ValueError(f"{name} 图片未完成：{row['image']}")
    translations = read_json(directory / "prompt_translations.json")
    if not isinstance(translations, dict) or any(
        not isinstance(translations.get(prompt), str) or not translations[prompt].strip()
        for prompt in prompts
    ):
        raise ValueError(f"{name} 的中文译文未完成。")


def audit_png(file):
    from PIL import Image

    with Image.open(file) as image:
        size = image.size
        image.verify()
    with file.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"不是有效的 PNG：{file.name}")
        while header := stream.read(8):
            length, kind = struct.unpack(">I4s", header)
            if kind not in {b"IHDR", b"IDAT", b"IEND"}:
                raise ValueError(f"PNG 含额外数据块 {kind!r}：{file.name}")
            stream.seek(length + 4, 1)  # Skip the chunk payload and CRC.
    return size


def audit_site(docs):
    catalog = read_json(docs / "results.json")
    expected = {name for name, _ in STATIC.values()} | {".nojekyll", "results.json"}
    cases = catalog["cases"]
    if not cases:
        raise ValueError("公开汇总中没有完成的结果。")
    count = 0
    for case in cases:
        metadata = list(case["image_metadata"].values()) + [case["metadata"]]
        for path in metadata:
            if not PUBLIC_JSON.fullmatch(path):
                raise ValueError("公开 JSON 路径无效。")
            read_json(docs / path)
            expected.add(path)
        for model, path in case["images"].items():
            if not PUBLIC_IMAGE.fullmatch(path):
                raise ValueError("公开图片路径无效。")
            size = case["image_sizes"][model]
            if audit_png(docs / path) != (size["width"], size["height"]):
                raise ValueError(f"图片尺寸与公开清单不一致：{path}")
            record = read_json(docs / case["image_metadata"][model])
            if (record["model"]["id"] != model or record["seed"] != case["seed"]
                    or record["width"] != size["width"] or record["height"] != size["height"]):
                raise ValueError(f"单图元信息与公开清单不一致：{path}")
            expected.add(path)
            count += 1
    if files_in(docs) != expected:
        raise ValueError("静态站点包含未列入公开清单的文件。")
    return len(cases), count


def audit_text(stage):
    local_names = {getpass.getuser(), Path.home().name} - {""}
    usernames = "|".join(re.escape(name) for name in sorted(local_names))
    patterns = {
        "本机用户名": rf"\b(?:{usernames})\b",
        "本机路径": r"(?<![a-z0-9])[a-z]:[\\/](?!/)|appdata[\\/]|[\\/]users[\\/]|/home/[\w.-]+/|\.codex[\\/]",
        "邮箱": r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b",
        "疑似凭据": r"\b(?:gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|sk-(?:proj-)?[a-z0-9_-]{24,}|akia[a-z0-9]{16})\b|-----BEGIN [A-Z ]*PRIVATE KEY-----",
    }
    for file in stage.rglob("*"):
        if not file.is_file() or file.suffix == ".png":
            continue
        value = file.read_text(encoding="utf-8-sig")
        for label, regex in patterns.items():
            if re.search(regex, value, re.IGNORECASE):
                raise ValueError(f"{file.relative_to(stage)} 包含{label}，请先审阅。")


def copy_sources(destination):
    prompts = tuple(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "prompts").rglob("*")
        if path.is_file() and path.suffix.lower() == ".txt"
    )
    for relative in (*SOURCE_FILES, *prompts):
        source = ROOT / relative
        if not source.resolve().is_relative_to(ROOT) or source.is_symlink():
            raise ValueError(f"源码路径不在 benchmark 内：{relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def install_release(stage, repo):
    verify_checkout(repo)
    # Retain the previous tracked snapshot in the temporary directory until both swaps succeed.
    old = stage / "previous"
    old.mkdir()
    try:
        for name in MANAGED:
            (repo / name).rename(old / name)
            (stage / name).rename(repo / name)
    except OSError:
        for name in reversed(MANAGED):
            if (old / name).exists():
                if (repo / name).exists():
                    (repo / name).rename(stage / name)
                (old / name).rename(repo / name)
        raise


def main():
    parser = argparse.ArgumentParser(description="导出并审查静态发布包，更新本地发布仓库。")
    parser.add_argument("--repo", type=Path, default=ROOT.parent / "benchmark-pages")
    parser.add_argument("--require-run", help="发布前校验此正式批次的图片和译文完整性")
    args = parser.parse_args()
    repo = args.repo.resolve()
    verify_checkout(repo)
    stage = Path(tempfile.mkdtemp(prefix=".benchmark-release-", dir=repo.parent)).resolve()
    if stage.parent != repo.parent:
        raise ValueError("暂存目录必须与发布仓库同级。")
    installed = False
    try:
        with benchmark_lock():
            if args.require_run:
                verify_batch(args.require_run)
            export_site(stage / "docs")
            (stage / "docs" / ".nojekyll").touch()
            copy_sources(stage / "benchmark")
        groups, count = audit_site(stage / "docs")
        audit_text(stage)
        install_release(stage, repo)
        installed = True
    finally:
        previous = stage / "previous"
        if installed or not previous.exists() or not any(previous.iterdir()):
            shutil.rmtree(stage)
        else:
            print(f"保留原发布文件备份：{previous}", file=sys.stderr)
    print(f"已准备 {groups} 组、{count} 张脱敏图片和对应 JSON。")
    print("请审阅发布仓库中的 Git 差异，然后提交并推送。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(f"发布包准备失败：{error}")
