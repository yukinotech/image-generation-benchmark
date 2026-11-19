"""Compare MiniMax per-image and staged execution on the same local ComfyUI."""

import argparse
import csv
import io
import msvcrt
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import (ROOT, RUNS, Comfy, atomic_write, inference_seconds, job_image_path,
                 png_size, preflight, read_json, run_job, write_json, write_report)
from workflows import make_minimax_staged_workflow


def reset_comfy_cache(comfy):
    queue = comfy.request("/queue")
    if queue["queue_running"] or queue["queue_pending"]:
        raise RuntimeError("A/B 需要空闲的 ComfyUI 队列。")
    comfy.request("/free", {"unload_models": True, "free_memory": True}, binary=True)
    # /free signals the worker asynchronously. Give it time to consume the flag.
    time.sleep(3)


def run_staged(comfy, run_dir, model, prompt, rows):
    jobs = [(row["width"], row["height"], row["seed"],
             f"benchmark/{run_dir.name}/B/{Path(row['image']).stem}") for row in rows]
    graph, outputs = make_minimax_staged_workflow(model, prompt, jobs)
    write_json(run_dir / "staged.api.json", graph)
    result = comfy.request("/prompt", {"prompt": graph, "prompt_id": str(uuid4())})
    prompt_id = result["prompt_id"]
    print(f"B · 提交 1 个工作流，覆盖 {len(rows)} 个尺寸 · {prompt_id}")
    history = comfy.wait(prompt_id, 3600)
    write_json(run_dir / "staged.history.json", history)
    status = history["status"]
    if status["status_str"] != "success":
        raise RuntimeError(f"B 运行失败：{status['messages'][-1]}")

    for row, output in zip(rows, outputs):
        image, = history["outputs"][output]["images"]
        query = urlencode({key: image[key] for key in ("filename", "subfolder", "type")})
        data = comfy.request("/view?" + query, binary=True)
        if png_size(data) != (row["width"], row["height"]):
            raise ValueError(f"B 的输出尺寸不符：{row['ratio']}")
        path = run_dir / row["image"]
        atomic_write(path, data)
        state = dict(status="ok", prompt_id=prompt_id, execution_mode="staged",
                     group_images=len(rows), group_seconds=inference_seconds(status))
        row.update(state)
        write_json(path.with_suffix(".json"), state)
    return inference_seconds(status)


def compare_images(run_dir, baseline, staged):
    metrics = []
    for a, b in zip(baseline, staged):
        with Image.open(run_dir / a["image"]) as original, Image.open(run_dir / b["image"]) as optimized:
            difference = ImageChops.difference(original.convert("RGB"), optimized.convert("RGB"))
        pixels = a["width"] * a["height"]
        hist = difference.histogram()
        absolute_error = sum((value % 256) * count for value, count in enumerate(hist))
        channels = difference.split()
        changed = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
        metrics.append({
            "ratio": a["ratio"], "width": a["width"], "height": a["height"],
            "seed": a["seed"], "baseline_seconds": a["seconds"],
            "pixel_exact": changed.getbbox() is None,
            "different_pixels_pct": round((pixels - changed.histogram()[0]) * 100 / pixels, 6),
            "rgb_mae_0_255": round(absolute_error / (pixels * 3), 6),
            "max_channel_error": max(hi for _, hi in difference.getextrema()),
        })
    with io.StringIO(newline="") as table:
        writer = csv.DictWriter(table, fieldnames=list(metrics[0]))
        writer.writeheader()
        writer.writerows(metrics)
        atomic_write(run_dir / "pixel_comparison.csv", table.getvalue().encode("utf-8-sig"))
    return metrics


def main():
    parser = argparse.ArgumentParser(description="MiniMax H3 Image 原流程和分阶段流程 A/B")
    parser.add_argument("--prompt", type=Path, default=ROOT / "prompts/03_chinese_typography.txt")
    args = parser.parse_args()
    prompt = args.prompt.read_bytes().decode("utf-8-sig")
    if not prompt.strip():
        raise ValueError("提示词为空。")
    config = read_json(ROOT / "config.json")
    model = config["models"]["minimax"]
    sizes = config["sizes"]
    seeds = config["seeds"]
    seed = secrets.randbits(64) if seeds is None else seeds[0]
    comfy = Comfy(config["server"])

    RUNS.mkdir(exist_ok=True)
    with (RUNS / ".lock").open("a+b") as lock:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        graphs = preflight(comfy, {"minimax": model}, sizes)
        order = comfy.request("/object_info/ImpactExecutionOrderController")
        if "ImpactExecutionOrderController" not in order:
            raise RuntimeError("分阶段流程需要项目中已安装的 Impact Pack 执行顺序节点。")
        run_dir = RUNS / ("minimax-ab-" + uuid4().hex[:8])
        run_dir.mkdir()
        print(f"A/B 目录 · {run_dir}")
        labels = {"minimax": model, "minimax-staged": model | {"label": "MiniMax H3 分阶段流程"}}
        labels["minimax"] = model | {"label": "MiniMax H3 原流程"}
        write_json(run_dir / "manifest.json", {
            "server": config["server"], "models": labels, "api_workflows": graphs,
            "model_order": list(labels), "prompt_order": [args.prompt.name], "ratio_order": list(sizes),
            "sizes": sizes, "seeds": [seed], "prompts": {args.prompt.name: prompt},
            "cache": "ComfyUI model and execution caches reset at the start of each arm; OS file cache untouched",
        })
        baseline, staged = [], []
        for index, (ratio, (width, height)) in enumerate(sizes.items(), 1):
            for model_id, rows in (("minimax", baseline), ("minimax-staged", staged)):
                rows.append(dict(model=model_id, prompt_file=args.prompt.name, prompt=prompt,
                                 ratio=ratio, width=width, height=height, pixels=width * height,
                                 seed=seed, image=job_image_path(model_id, 1, index, width, height, seed),
                                 status="waiting", **{key: model[key] for key in
                                                      ("steps", "cfg", "sampler", "scheduler")}))
        try:
            reset_comfy_cache(comfy)
            start = time.monotonic()
            for row in baseline:
                row.update(run_job(comfy, run_dir, row, model, 3600))
                if row["status"] != "ok":
                    raise RuntimeError(f"A 运行失败：{row.get('error')}")
                print(f"A · {row['ratio']} · {row['seconds']} 秒")
            a_wall = time.monotonic() - start

            reset_comfy_cache(comfy)
            start = time.monotonic()
            b_seconds = run_staged(comfy, run_dir, model, prompt, staged)
            b_wall = time.monotonic() - start
            metrics = compare_images(run_dir, baseline, staged)
            a_seconds = sum(row["seconds"] for row in baseline)
            summary = {
                "images_per_arm": len(baseline),
                "baseline_comfy_seconds": round(a_seconds, 3),
                "staged_comfy_seconds": b_seconds,
                "comfy_time_reduction_pct": round((1 - b_seconds / a_seconds) * 100, 2),
                "baseline_wall_seconds": round(a_wall, 3),
                "staged_wall_seconds": round(b_wall, 3),
                "pixel_exact_pairs": sum(row["pixel_exact"] for row in metrics),
                "pixel_metrics": metrics,
            }
            write_json(run_dir / "summary.json", summary)
            print(f"A · {a_seconds:.2f} 秒 / B · {b_seconds:.2f} 秒 · 像素完全一致 {summary['pixel_exact_pairs']}/{len(metrics)}")
        finally:
            write_report(run_dir, baseline + staged, labels)
            print(f"对比报告 · {run_dir / 'report.html'}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        print(f"运行中止：{error}", file=sys.stderr)
        sys.exit(1)
