import argparse
import csv
import hashlib
import html
import io
import json
import math
import msvcrt
import re
import secrets
import struct
import sys
import time
from contextlib import contextmanager
from itertools import groupby, product
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))  # The portable Python ._pth isolates script directories.
from artifacts import RUNS, job_image_path, read_json, replace_artifact
from workflows import OUTPUT, make_minimax_staged_workflow, make_workflow

SMOKE_TEXT = 'A cobalt blue ceramic teapot beside two yellow lemons on a linen tablecloth, soft morning window light, editorial still-life photograph.'


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    temp.write_bytes(data)
    replace_artifact(temp, path)


def write_json(path, data):
    atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


class Comfy:
    def __init__(self, server):
        self.server = server.rstrip("/")

    def request(self, path, data=None, binary=False):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = Request(self.server + path, data=body, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                result = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"ComfyUI {path} HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"无法访问 ComfyUI {self.server}{path}：{exc}") from exc
        return result if binary else json.loads(result)

    def history(self, prompt_id):
        return self.request(f"/history/{prompt_id}").get(prompt_id)

    def find_job(self, prompt_id):
        queue = self.request("/queue")
        if any(job[1] == prompt_id for job in queue["queue_running"] + queue["queue_pending"]):
            return True
        return self.history(prompt_id) is not None

    def wait(self, prompt_id, timeout):
        deadline = time.monotonic() + timeout
        while True:
            history = self.history(prompt_id)
            if history is not None:
                return history
            if time.monotonic() >= deadline:
                raise RuntimeError(f"等待超时，任务 {prompt_id} 可能仍在运行。用相同命令可继续等待。")
            time.sleep(2)


def options(node_info, field):
    for group in ("required", "optional"):
        if field in node_info["input"].get(group, {}):
            spec = node_info["input"][group][field]
            if isinstance(spec[0], list):
                return spec[0]
            if spec[0] == "COMBO":
                return spec[1]["options"]
    return None


def preflight(comfy, models, sizes, model_sizes=None):
    info = comfy.request("/object_info")
    if "minimax" in models and "ImpactExecutionOrderController" not in info:
        raise ValueError("MiniMax 分阶段流程需要 Impact Pack 的执行顺序节点。")
    errors = []
    graphs = {}
    for model_id, model in models.items():
        try:
            for key, node, field in (
                ("unet", "UNETLoader", "unet_name"),
                ("clip", "CLIPLoader", "clip_name"),
                ("vae", "VAELoader", "vae_name"),
            ):
                expected = model[key].replace("\\", "/")
                available = options(info[node], field)
                model[key] = next((name for name in available if name.replace("\\", "/") == expected), None)
                if model[key] is None:
                    raise ValueError(f"模型文件未被 ComfyUI 识别：{expected}")

            planned_sizes = (model_sizes or {}).get(model_id, sizes)
            for width, height in planned_sizes.values():
                graph = make_workflow(model_id, model, "", width, height, 0, "benchmark")
                for node in graph.values():
                    name = node["class_type"]
                    schema = info[name]
                    fields = schema["input"]
                    allowed = fields.get("required", {}) | fields.get("optional", {})
                    required = {key for key, spec in fields.get("required", {}).items()
                                if spec[0] != "COMFY_AUTOGROW_V3" or spec[1]["template"]["min"] > 0}
                    missing = required - node["inputs"].keys()
                    if missing:
                        raise ValueError(f"{name} 缺少必填输入 {', '.join(sorted(missing))}")
                    for key, value in node["inputs"].items():
                        if key not in allowed:
                            raise ValueError(f"{name} 不支持输入 {key}")
                        choices = options(schema, key)
                        if choices is not None and value not in choices:
                            raise ValueError(f"{name}.{key} 不支持 {value!r}")
                        spec = allowed[key]
                        if spec[0] == "INT" and isinstance(value, int):
                            bounds = spec[1]
                            if not bounds.get("min", value) <= value <= bounds.get("max", value):
                                raise ValueError(f"{name}.{key} 超出范围：{value}")
            sample = next(iter(planned_sizes.values())) if model_id in (model_sizes or {}) else (1024, 1024)
            graphs[model_id] = make_workflow(model_id, model, "", *sample, 0, "benchmark")
            print(f"通过预检  {model_id}  {model['unet']}")
        except KeyError as exc:
            errors.append(f"{model_id}: 缺少节点或配置字段 {exc}")
        except ValueError as exc:
            errors.append(f"{model_id}: {exc}")
    if errors:
        raise ValueError("\n".join(errors))
    return graphs


def load_prompts(directory, smoke):
    if smoke:
        return {"smoke.txt": SMOKE_TEXT}
    if not directory.is_dir():
        raise ValueError(f"提示词目录不存在：{directory}")
    prompts = {}
    for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_file() and path.suffix.lower() == ".txt":
            try:
                value = path.read_bytes().decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError(f"请将提示词另存为 UTF-8：{path}") from exc
            if value.strip():
                prompts[path.relative_to(directory).as_posix()] = value
    return prompts


def load_size_rules(directory, prompts, configured_sizes):
    path = directory / "size_rules.json"
    try:
        rules = read_json(path)
    except FileNotFoundError:
        return {}
    if not isinstance(rules, dict):
        raise ValueError(f"{path} 应为提示词相对路径到宽高比的 JSON 对象。")
    for name, ratio in rules.items():
        if name not in prompts:
            raise ValueError(f"{path} 引用了不存在或为空的提示词：{name}")
        if not isinstance(ratio, str) or ratio not in configured_sizes:
            raise ValueError(f"{path} 中 {name} 的宽高比未在配置的 sizes 中定义：{ratio!r}")
    return rules


def png_size(data):
    if (len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n"
            or not data.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82")):
        raise ValueError("ComfyUI 返回的 PNG 不完整")
    return struct.unpack(">II", data[16:24])


def inference_seconds(status):
    events = {name: payload.get("timestamp") for name, payload in status.get("messages", [])}
    start, end = events.get("execution_start"), events.get("execution_success")
    return round((end - start) / 1000, 3) if start is not None and end is not None else ""


def image_complete(run_dir, row, state):
    if state.get("status") != "ok":
        return False
    try:
        return png_size((run_dir / row["image"]).read_bytes()) == (row["width"], row["height"])
    except (FileNotFoundError, ValueError):
        return False


def execution_error(status):
    return next((payload.get("exception_message", name)
                 for name, payload in reversed(status.get("messages", []))
                 if name in {"execution_error", "execution_interrupted"}), str(status))


def save_output_image(comfy, run_dir, row, images):
    if len(images) != 1:
        raise ValueError(f"预期 1 张图，实际收到 {len(images)} 张")
    image = images[0]
    query = urlencode({key: image[key] for key in ("filename", "subfolder", "type")})
    data = comfy.request("/view?" + query, binary=True)
    actual = png_size(data)
    if actual != (row["width"], row["height"]):
        raise ValueError(f"实际尺寸 {actual[0]}×{actual[1]} 与目标不同")
    atomic_write(run_dir / row["image"], data)


def run_job(comfy, run_dir, row, model, timeout):
    image_path = run_dir / row["image"]
    state_path = image_path.with_suffix(".json")
    state = read_json(state_path) if state_path.exists() else {}
    if image_complete(run_dir, row, state):
        return state

    prompt_id = state.get("prompt_id")
    if not prompt_id or state.get("status") == "failed" or not comfy.find_job(prompt_id):
        prompt_id = str(uuid4())
        state = {"status": "pending", "prompt_id": prompt_id}
        write_json(state_path, state)
        prefix = f"benchmark/{run_dir.name}/{row['model']}/{image_path.stem}"
        graph = make_workflow(row["model"], model, row["prompt"], row["width"],
                              row["height"], row["seed"], prefix)
        queued = comfy.request("/prompt", {"prompt": graph, "prompt_id": prompt_id})
        if queued["prompt_id"] != prompt_id:
            prompt_id = queued["prompt_id"]
            state["prompt_id"] = prompt_id
            write_json(state_path, state)

    history = comfy.wait(prompt_id, timeout)
    status = history.get("status", {})
    if status.get("status_str") != "success":
        state.update(status="failed", error=execution_error(status))
    else:
        images = history.get("outputs", {}).get(OUTPUT, {}).get("images", [])
        try:
            save_output_image(comfy, run_dir, row, images)
            state.update(status="ok", seconds=inference_seconds(status))
        except ValueError as exc:
            state.update(status="failed", error=str(exc))

    write_json(state_path, state)
    return state


def run_minimax_prompt(comfy, run_dir, rows, model, timeout, *, sized_groups=False):
    missing = [row for row in rows if not image_complete(run_dir, row, row)]
    if not missing:
        return

    # Recover any single-image job submitted before the workflow migration.
    for row in missing:
        if (row["status"] == "pending" and not row.get("execution_mode")
                and row.get("prompt_id") and comfy.find_job(row["prompt_id"])):
            row.update(run_job(comfy, run_dir, row, model, timeout))
    missing = [row for row in missing if not image_complete(run_dir, row, row)]
    if not missing:
        return

    prompt_number = Path(rows[0]["image"]).stem.split("-", 1)[0]
    if sized_groups:
        paths = "\n".join(row["image"] for row in rows)
        prompt_number += "-" + hashlib.sha256(paths.encode("utf-8")).hexdigest()[:12]
    group_path = run_dir / "minimax-groups" / f"{prompt_number}.json"
    group = read_json(group_path) if group_path.exists() else {}
    if group.get("status") != "pending" or not comfy.find_job(group["prompt_id"]):
        jobs = [(row["width"], row["height"], row["seed"],
                 f"benchmark/{run_dir.name}/minimax/{Path(row['image']).stem}") for row in missing]
        graph, output_nodes = make_minimax_staged_workflow(model, rows[0]["prompt"], jobs)
        group = {
            "status": "pending",
            "prompt_id": str(uuid4()),
            "outputs": {row["image"]: output for row, output in zip(missing, output_nodes)},
            "api_workflow": graph,
        }
        # Record all output IDs before submitting the shared ComfyUI job.
        write_json(group_path, group)
        queued = comfy.request("/prompt", {"prompt": graph, "prompt_id": group["prompt_id"]})
        group["prompt_id"] = queued["prompt_id"]
        write_json(group_path, group)

    prompt_id = group["prompt_id"]
    pending = [row for row in missing if row["image"] in group["outputs"]]
    print(f"MiniMax 分阶段 · {rows[0]['prompt_file']} · {len(group['outputs'])} 张 · {prompt_id}")
    common = dict(execution_mode="staged", prompt_id=prompt_id, group_images=len(group["outputs"]))
    for row in pending:
        write_json((run_dir / row["image"]).with_suffix(".json"), common | {"status": "pending"})

    history = comfy.wait(prompt_id, timeout)
    status = history.get("status", {})
    success = status.get("status_str") == "success"
    seconds = inference_seconds(status) if success else ""
    for row in pending:
        output = group["outputs"][row["image"]]
        images = history.get("outputs", {}).get(output, {}).get("images", [])
        state = common | {"group_seconds": seconds}
        try:
            if not success and not images:
                raise ValueError(execution_error(status))
            save_output_image(comfy, run_dir, row, images)
            state["status"] = "ok"
            row.pop("error", None)
            row.pop("seconds", None)
        except ValueError as exc:
            state.update(status="failed", error=str(exc))
        write_json((run_dir / row["image"]).with_suffix(".json"), state)
        row.update(state)

    group.update(status="ok" if success and all(row["status"] == "ok" for row in pending)
                 else "failed", seconds=seconds)
    write_json(group_path, group)
    print(f"  {sum(row['status'] == 'ok' for row in pending)}/{len(pending)} 张已保存 · 整组 {seconds} 秒")


def write_report(run_dir, rows, models):
    rows = [row | {
        "image": row["image"] if row["status"] == "ok" else "",
        "execution_mode": row.get("execution_mode", "single" if row["status"] == "ok" else ""),
    } for row in rows]
    table = io.StringIO(newline="")
    fields = ["model", "prompt_file", "prompt", "ratio", "width", "height", "pixels", "seed",
              "steps", "cfg", "sampler", "scheduler", "status", "seconds", "image", "prompt_id", "error",
              "execution_mode", "group_seconds", "group_images"]
    writer = csv.DictWriter(table, fieldnames=fields, restval="", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(run_dir / "results.csv", table.getvalue().encode("utf-8-sig"))

    esc = html.escape
    groups = {}
    for row in rows:
        key = (row["prompt_file"], row["ratio"], row["seed"])
        groups.setdefault(key, []).append(row)
    done = sum(row["status"] == "ok" for row in rows)
    page = ["""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>文生图 Benchmark</title>
<style>
*{box-sizing:border-box}body{background:#161719;color:#eee;font:15px system-ui;margin:24px}
section{border-top:1px solid #555;margin-top:24px;padding-top:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
figure{margin:0;background:#25272b;padding:8px}img{width:100%;height:auto;display:block}
figcaption{margin-top:8px}a{color:#9cd4ff}pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style><body><h1>文生图 Benchmark</h1>""",
            f'<p>完成 {done} / {len(rows)} 张 · <a href="results.csv">CSV</a> · '
            '<a href="manifest.json">运行参数</a></p>']
    for (prompt_file, ratio, seed), group in groups.items():
        page.append(f"<section><h2>{esc(prompt_file)} · {esc(ratio)} · seed {seed}</h2>"
                    f"<details><summary>提示词</summary><pre>{esc(group[0]['prompt'])}</pre></details>"
                    '<div class="grid">')
        for row in group:
            label = esc(models[row["model"]]["label"])
            width, height = row["width"], row["height"]
            if row["image"]:
                src = esc(row["image"])
                content = (f'<a href="{src}"><img loading="lazy" src="{src}" alt="{label}" '
                           f'width="{width}" height="{height}"></a>')
                seconds = row.get("seconds", "")
                note = f" · {seconds}s" if seconds != "" else ""
                if row["execution_mode"] == "staged":
                    group_seconds = row.get("group_seconds", "")
                    note = " · 分阶段"
                    if group_seconds != "":
                        note += f" · 整组 {row['group_images']} 张 / {group_seconds}s"
            else:
                status_text = "排队中 / 待续跑" if row["status"] == "pending" else "未运行"
                content = f"<pre>{esc(row.get('error') or status_text)}</pre>"
                note = ""
            page.append(f"<figure>{content}<figcaption>{label} · {width}×{height} · "
                        f"{row['steps']} 步{note}</figcaption></figure>")
        page.append("</div></section>")
    page.append("</body></html>")
    atomic_write(run_dir / "report.html", "\n".join(page).encode("utf-8"))


def run_batch(comfy, run_dir, manifest, rows, timeout, resume_models=None, expected_manifest=None,
              *, publish_when_complete=False):
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        previous = read_json(manifest_path)
        legacy = {key: value for key, value in manifest.items() if key != "minimax_execution"}
        if expected_manifest is not None and previous != expected_manifest:
            raise ValueError("批次清单在准备续跑时已更新，请重新执行命令。")
        if expected_manifest is None and previous != manifest and previous != legacy:
            raise ValueError("该运行名已有不同的提示词或参数。请用 --run 指定新名称。")
    elif resume_models:
        raise ValueError("--resume-models 需要指定已有批次。")
    if not publish_when_complete:
        write_json(manifest_path, manifest)
    models = manifest["models"]
    for row in rows:
        state_path = (run_dir / row["image"]).with_suffix(".json")
        if state_path.exists():
            row.update(read_json(state_path))
    selected = [(index, row) for index, row in enumerate(rows, 1)
                if resume_models is None or row["model"] in resume_models]
    try:
        for (model_id, _), jobs in groupby(
            selected, key=lambda entry: (entry[1]["model"], entry[1]["prompt_file"])
        ):
            if model_id == "minimax":
                run_minimax_prompt(comfy, run_dir, [row for _, row in jobs], models[model_id], timeout,
                                   sized_groups="minimax" in manifest.get("model_sizes", {}))
                continue
            for index, row in jobs:
                print(f"[{index}/{len(rows)}] {row['model']} · {row['prompt_file']} · "
                  f"{row['width']}x{row['height']} · seed {row['seed']}", flush=True)
                state = run_job(comfy, run_dir, row, models[model_id], timeout)
                row.pop("error", None)
                row.update(state)
                print(f"  {state['status']} {state.get('error') or state.get('seconds', '')}", flush=True)
    finally:
        for row in rows:
            state_path = (run_dir / row["image"]).with_suffix(".json")
            if state_path.exists():
                row.update(read_json(state_path))
        completion_rows = rows if publish_when_complete else (row for _, row in selected)
        complete = all(image_complete(run_dir, row, row) for row in completion_rows)
        if not publish_when_complete or complete:
            write_report(run_dir, rows, models)
            if publish_when_complete:
                write_json(manifest_path, manifest)
            print(f"对比报告：{run_dir / 'report.html'}")
    return 0 if complete else 1


def run_benchmark(args, run_name):
    config = read_json(args.config)
    run_dir = RUNS / run_name
    if args.resume_models:
        if args.check or args.dry_run or args.smoke or args.models or args.ratios or args.exclude_prompts:
            raise ValueError("--resume-models 使用已保存的批次设置，不能与模型、尺寸或提示词筛选参数同用。")
        if not (run_dir / "manifest.json").is_file():
            raise ValueError("--resume-models 需要指定已有批次。")
        return resume_batch(args, run_dir, config)

    models = {key: value for key, value in config["models"].items()
              if args.models is None or key in args.models}
    ratios = args.ratios or (["1:1"] if args.smoke else list(config["sizes"]))
    sizes = {key: config["sizes"][key] for key in ratios}
    seeds = config["seeds"]
    if seeds is None:
        manifest_path = run_dir / "manifest.json"
        seeds = (read_json(manifest_path)["seeds"] if manifest_path.is_file()
                 else [secrets.randbits(64)])
    if args.smoke:
        seeds = seeds[:1]
    if not models or set(args.models or models) - models.keys():
        raise ValueError("模型 ID 可选：" + ", ".join(config["models"]))
    if not sizes or any(len(size) != 2 or any(type(n) != int or n < 32 or n % 32
                                             for n in size) for size in sizes.values()):
        raise ValueError("宽度、高度都必须是至少 32 的 32 倍数。")
    if not seeds or len(set(seeds)) != len(seeds) or any(type(s) != int or not 0 <= s < 2**64 for s in seeds):
        raise ValueError("seeds 应为不重复的 64 位无符号整数。")
    if args.timeout <= 0:
        raise ValueError("--timeout 必须大于 0。")
    model_sizes = calculate_model_sizes(models, sizes, config)
    server = args.server or config["server"]
    comfy = Comfy(server)
    if args.check:
        preflight(comfy, models, sizes, model_sizes)
        return 0

    if args.smoke and len(sizes) != 1:
        raise ValueError("--smoke 仅支持一个宽高比。")
    prompts = load_prompts(args.prompts, args.smoke)
    if not prompts:
        raise ValueError(f"请在 {args.prompts} 放入至少一个非空 UTF-8 .txt 文件。")
    size_rules = {} if args.smoke else load_size_rules(args.prompts, prompts, config["sizes"])
    excluded = set(args.exclude_prompts or ())
    unknown = excluded - prompts.keys()
    if unknown:
        raise ValueError("找不到要跳过的提示词：" + ", ".join(sorted(unknown)))
    prompts = {name: prompt for name, prompt in prompts.items() if name not in excluded}
    if not prompts:
        raise ValueError("--exclude-prompts 排除了全部提示词。")
    size_rules = {name: ratio for name, ratio in size_rules.items() if name in prompts}
    for name, ratio in size_rules.items():
        if ratio in sizes:
            print(f"固定比例：{name} · {ratio}")
        else:
            print(f"跳过：{name} 要求 {ratio}，本批次未选择该比例。")
    prompts = {name: prompt for name, prompt in prompts.items()
               if name not in size_rules or size_rules[name] in sizes}
    size_rules = {name: ratio for name, ratio in size_rules.items() if name in prompts}
    if not prompts:
        raise ValueError("所选比例与所有提示词的固定尺寸规则均不匹配。")
    rows = plan_rows(models, prompts, sizes, seeds, size_rules, model_sizes)
    case_count = len(rows) // (len(models) * len(seeds))
    print(f"{len(models)} 个模型 × {case_count} 组提示词与尺寸 × {len(seeds)} 个 seed = {len(rows)} 张")
    if args.dry_run:
        for ratio, (width, height) in sizes.items():
            print(f"{ratio}: {width}x{height} ({width * height:,} px)")
        for model, overrides in model_sizes.items():
            for ratio, (width, height) in overrides.items():
                print(f"{model} · {ratio}: {width}x{height} ({width * height:,} px)")
        return 0
    with benchmark_lock():
        graphs = preflight(comfy, models, sizes, model_sizes)
        manifest = dict(server=server, models=models, api_workflows=graphs, sizes=sizes, seeds=seeds,
                        prompts=prompts, model_order=list(models), prompt_order=list(prompts),
                        ratio_order=list(sizes))
        if size_rules:
            manifest["size_rules"] = size_rules
        if model_sizes:
            manifest["model_sizes"] = model_sizes
        if "minimax" in models:
            manifest["minimax_execution"] = {
                "mode": "staged",
                "group_by": "prompt_file",
                "run_records": "minimax-groups/",
                "legacy_images": "preserve successful images generated by the single-image workflow",
            }
        status = run_batch(comfy, run_dir, manifest, rows, args.timeout)
        return finish_translation(args, run_dir, rows, status)


def calculate_model_sizes(models, sizes, config):
    overrides = {}
    budgets = config.get("model_pixel_budgets", {})
    presets = config.get("model_size_presets", {})
    for model, pixels in budgets.items():
        if model not in models:
            continue
        if type(pixels) is not int or pixels <= 0:
            raise ValueError(f"{model} 的像素预算必须为正整数。")
        overrides[model] = {}
        for ratio in sizes:
            preset = presets.get(model, {}).get(ratio)
            if preset is not None:
                if len(preset) != 2 or any(type(side) is not int or side < 32 or side % 32 for side in preset):
                    raise ValueError(f"{model} 的 {ratio} 预设宽高必须为 32 的正整数倍。")
                overrides[model][ratio] = preset
                continue
            try:
                horizontal, vertical = map(int, ratio.split(":"))
                if min(horizontal, vertical) <= 0:
                    raise ValueError
            except ValueError as exc:
                raise ValueError(f"{model} 的像素预算需要有效的宽高比：{ratio}") from exc
            aspect = horizontal / vertical
            sides = (math.sqrt(pixels * aspect), math.sqrt(pixels / aspect))
            overrides[model][ratio] = [max(32, round(side / 32) * 32) for side in sides]
    return overrides


def plan_rows(models, prompts, sizes, seeds, size_rules, model_sizes=None):
    rows = []
    for model_id, (prompt_index, (prompt_file, prompt)), (ratio_index, ratio), seed in product(
        models, enumerate(prompts.items(), 1), enumerate(sizes, 1), seeds
    ):
        if prompt_file in size_rules and size_rules[prompt_file] != ratio:
            continue
        model = models[model_id]
        width, height = (model_sizes or {}).get(model_id, sizes)[ratio]
        image = job_image_path(model_id, prompt_index, ratio_index, width, height, seed)
        rows.append(dict(model=model_id, prompt_file=prompt_file, prompt=prompt, ratio=ratio,
                         width=width, height=height, pixels=width * height, seed=seed,
                         steps=model["steps"], cfg=model["cfg"], sampler=model["sampler"],
                         scheduler=model["scheduler"], image=image, status="waiting"))
    return rows


@contextmanager
def benchmark_lock():
    RUNS.mkdir(exist_ok=True)
    with (RUNS / ".lock").open("a+b") as lock:
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("已有 benchmark 进程在运行。") from exc
        yield


def resume_batch(args, run_dir, config):
    with benchmark_lock():
        previous = read_json(run_dir / "manifest.json")
        saved = previous["models"]
        configured_models = config["models"]
        requested = set(args.resume_models)
        unknown = requested - saved.keys() - configured_models.keys()
        if unknown:
            raise ValueError("未知模型 ID：" + ", ".join(sorted(unknown)))
        additions = {key: model.copy() for key, model in configured_models.items()
                     if key in requested and key not in saved}
        models = {key: saved[key] for key in previous.get("model_order", saved)} | additions
        prompts = {key: previous["prompts"][key] for key in previous.get("prompt_order", previous["prompts"])}
        sizes = {key: previous["sizes"][key] for key in previous.get("ratio_order", previous["sizes"])}
        comfy = Comfy(args.server or previous["server"])
        manifest = previous
        overrides = previous.get("model_sizes", {})
        size_rules = previous.get("size_rules", {})
        refreshed = {}
        if args.refresh_model_sizes:
            requested_models = {key: models[key].copy() for key in models if key in requested}
            refreshed = calculate_model_sizes(requested_models, sizes, config)
            if set(refreshed) != requested:
                raise ValueError("--refresh-model-sizes 需要为所选模型配置像素预算。")
            overrides = overrides | refreshed
            current_prompts = load_prompts(args.prompts, False)
            current_rules = load_size_rules(args.prompts, current_prompts, config["sizes"])
            size_rules = size_rules | {prompt: current_rules[prompt] for prompt in prompts if prompt in current_rules}
            missing_ratios = set(size_rules.values()) - sizes.keys()
            if missing_ratios:
                raise ValueError("批次缺少当前指定的画幅：" + ", ".join(sorted(missing_ratios)))
        checks = additions | {key: models[key].copy() for key in refreshed}
        if checks:
            graphs = previous["api_workflows"] | preflight(comfy, checks, sizes, overrides)
            manifest = previous | dict(models=models, model_order=list(models), api_workflows=graphs)
            if overrides:
                manifest["model_sizes"] = overrides
            if size_rules:
                manifest["size_rules"] = size_rules
        rows = plan_rows(models, prompts, sizes, previous["seeds"], size_rules, overrides)
        print(f"{run_dir.name}：补跑 {sum(row['model'] in requested for row in rows)} 张，"
              f"汇总共 {len(rows)} 张。")
        status = run_batch(comfy, run_dir, manifest, rows, args.timeout, requested, previous,
                           publish_when_complete=args.refresh_model_sizes)
        if status and args.refresh_model_sizes:
            print("重绘结果尚未全部通过校验，继续使用上一次发布的批次清单。", file=sys.stderr)
        return finish_translation(args, run_dir, rows, status)


def finish_translation(args, run_dir, rows, status):
    if args.no_translate or status != 0:
        return status
    if not all(image_complete(run_dir, row, row) for row in rows):
        print("整批图片完成后会自动补齐中文译文。")
        return status
    from translate_prompts import translate_local_run
    try:
        output = translate_local_run(run_dir)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"图片、HTML 和 CSV 已保存。自动翻译失败：{error}", file=sys.stderr)
        print(f"可用 benchmark\\translate_prompts.cmd --run {run_dir.name} 单独重试。", file=sys.stderr)
        return 1
    print(f"中文提示词：{output.resolve()}")
    return status


def main():
    parser = argparse.ArgumentParser(description="用本机 ComfyUI 顺序跑文生图对比，完成后生成中文译文")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--server", help="覆盖 ComfyUI URL")
    parser.add_argument("--prompts", type=Path, default=ROOT / "prompts")
    parser.add_argument("--exclude-prompts", nargs="+", help="跳过指定的提示词相对路径")
    parser.add_argument("--run", help="运行目录名。默认 main，冒烟测试默认 smoke")
    parser.add_argument("--models", nargs="+", help="只跑指定模型，顺序与配置一致")
    parser.add_argument("--resume-models", nargs="+", help="按已有批次清单续跑或添加指定模型")
    parser.add_argument("--refresh-model-sizes", action="store_true",
                        help="续跑时按当前像素预算重绘所选模型，全部成功后更新批次")
    parser.add_argument("--ratios", nargs="+", help="只跑指定宽高比，例如 1:1 16:9")
    parser.add_argument("--timeout", type=int, default=3600, help="单图等待秒数")
    parser.add_argument("--no-translate", action="store_true", help="跳过整批完成后的本地 Qwen 翻译")
    parser.add_argument("--with-view", action="store_true", help="运行时同时提供看板，完成后继续提供直到 Ctrl+C")
    parser.add_argument("--port", type=int, default=8765, help="看板端口，0 自动选择")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只检查服务、模型文件和工作流输入")
    mode.add_argument("--dry-run", action="store_true", help="只显示批次计划和预计张数")
    mode.add_argument("--smoke", action="store_true", help="用内置提示词，每个模型各生成一张")
    mode.add_argument("--view", action="store_true", help="只启动看板")
    args = parser.parse_args()
    if args.refresh_model_sizes and not args.resume_models:
        parser.error("--refresh-model-sizes 需要 --resume-models")
    run_name = args.run or ("smoke" if args.smoke else "main")
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", run_name)
            or re.fullmatch(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", run_name, re.I)):
        parser.error("--run 需要使用有效的 Windows 目录名，仅允许字母、数字、下划线和连字符")
    if not 0 <= args.port <= 65535:
        parser.error("--port 应在 0 到 65535 之间")
    if args.with_view and (args.view or args.check or args.dry_run):
        parser.error("--with-view 需要运行批次，查看已有结果请用 --view")
    if args.view or args.with_view:
        from frontend.serve import open_viewer, serve_in_background
        with open_viewer(args.port) as viewer:
            if args.view:
                try:
                    viewer.serve_forever()
                except KeyboardInterrupt:
                    pass
                return 0
            with serve_in_background(viewer):
                status = run_benchmark(args, run_name)
                print("看板仍可访问，按 Ctrl+C 关闭。", flush=True)
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    return status
    return run_benchmark(args, run_name)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        print(f"运行中止：{error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("已停止提交。ComfyUI 当前任务可继续完成，用相同命令恢复。", file=sys.stderr)
        sys.exit(130)
