"""Serve the read-only benchmark viewer and its local run data."""

import argparse
import hashlib
import errno
import json
import re
import shutil
import socket
import sys
from contextlib import contextmanager
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from itertools import product
from pathlib import Path
from threading import Thread
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from artifacts import RUNS, job_image_path, read_json

RUN_NAME = re.compile(r"[A-Za-z0-9_-]{1,48}", re.ASCII)
IMAGE = re.compile(r"images/[\w-]+/p[0-9]+-r[0-9]+-[0-9]+x[0-9]+-s[0-9]+\.png", re.ASCII)
REFERENCE_MODELS = {
    "gpt-image": "GPT 生图",
    "mai-image-2-7-flash": "MAI Image 2.7 Flash",
}
REFERENCE_IMAGE = re.compile(
    r"references/[a-f0-9]{20}/(?:" + "|".join(map(re.escape, REFERENCE_MODELS)) + r")\.png",
    re.ASCII,
)
WEBP_VARIANTS = {
    "thumbnail": ("-thumb.webp", 768, 82),
    "preview": ("-preview.webp", 2048, 88),
}
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
REPRODUCTION_FIELDS = ("unet", "clip", "vae", "steps", "cfg", "sampler", "scheduler", "negative", "template")
WORKFLOW_FIELDS = {
    "KSampler": ("denoise",),
    "ModelSamplingAuraFlow": ("shift",),
    "TextEncodeQwenImage21": ("resolution", "negative_prompt"),
    "QwenImage21Cache": ("device", "dtype"),
}


def generation_parameters(model, graph, state):
    parameters = {key: model[key] for key in REPRODUCTION_FIELDS if key in model}
    parameters["execution_mode"] = state.get("execution_mode", "single")
    options = {}
    for node in graph.values():
        kind = node["class_type"]
        if kind in WORKFLOW_FIELDS:
            inputs = node["inputs"]
            options[kind] = {key: inputs[key] for key in WORKFLOW_FIELDS[kind] if key in inputs}
    if options:
        parameters["workflow_options"] = options
    return parameters


def run_dirs():
    if not RUNS.is_dir():
        return []
    return sorted(
        (path.name for path in RUNS.iterdir()
         if RUN_NAME.fullmatch(path.name) and path.resolve().parent == RUNS.resolve()
         and (path / "manifest.json").is_file()),
        key=lambda name: (name != "main", name.casefold()),
    )


def webp_paths(original):
    return {label: original.removesuffix(".png") + suffix
            for label, (suffix, _, _) in WEBP_VARIANTS.items()}


def public_catalog(*, static=False):
    from PIL import Image

    models = {}
    cases = []
    assets = {}
    metadata = {}
    for name in run_dirs():
        directory = RUNS / name
        manifest = read_json(directory / "manifest.json")
        if name.startswith("minimax-ab-") or list(manifest["prompts"]) == ["smoke.txt"]:
            continue

        prompts = manifest["prompts"]
        model_order = manifest.get("model_order", list(manifest["models"]))
        prompt_order = manifest.get("prompt_order", list(prompts))
        sizes = manifest["sizes"]
        model_sizes = manifest.get("model_sizes", {})
        ratio_order = manifest.get("ratio_order", list(sizes))
        size_rules = manifest.get("size_rules", {})
        try:
            translations = read_json(directory / "prompt_translations.json")
        except (OSError, ValueError):
            translations = {}
        if not isinstance(translations, dict):
            translations = {}

        for (prompt_index, prompt), (ratio_index, ratio), seed in product(
            enumerate(prompt_order, 1), enumerate(ratio_order, 1), manifest["seeds"]
        ):
            if prompt in size_rules and size_rules[prompt] != ratio:
                continue
            width, height = sizes[ratio]
            case_key = json.dumps([name, prompt, ratio, width, height, seed])
            case_id = hashlib.sha256(case_key.encode("utf-8")).hexdigest()[:20]
            images = {}
            image_previews = {}
            image_sizes = {}
            image_metadata = {}
            parameters = {}
            for model in model_order:
                image_width, image_height = model_sizes.get(model, sizes)[ratio]
                relative = job_image_path(model, prompt_index, ratio_index, image_width, image_height, seed)
                path = directory / relative
                if (not IMAGE.fullmatch(relative) or not path.is_file()
                        or not path.resolve().is_relative_to(directory.resolve())):
                    continue
                try:
                    state = read_json(path.with_suffix(".json"))
                except FileNotFoundError:
                    continue
                if state.get("status") != "ok":
                    continue
                settings = manifest["models"][model]
                models.setdefault(model, settings["label"])
                graph = manifest.get("api_workflows", {}).get(model, {})
                parameters[model] = generation_parameters(settings, graph, state)
                image_sizes[model] = {"width": image_width, "height": image_height}
                metadata_path = f"metadata/{case_id}/{model}.json"
                metadata[metadata_path] = {
                    "case_id": case_id, "prompt": prompts[prompt], "ratio": ratio, "seed": str(seed),
                    **image_sizes[model], "pixels": image_width * image_height,
                    "model": {"id": model, "label": settings["label"], **parameters[model]},
                }
                image_metadata[model] = metadata_path
                if static:
                    url = f"images/{case_id}/{model}.png"
                    assets[url] = path
                else:
                    url = f"runs/{quote(name, safe='')}/{quote(relative)}"
                images[model] = url
                image_previews[model] = webp_paths(url)
            if images:
                translation = translations.get(prompt)
                metadata_path = f"metadata/{case_id}.json"
                distinct_sizes = {(size["width"], size["height"]) for size in image_sizes.values()}
                shared_dimensions = next(iter(image_sizes.values())) if len(distinct_sizes) == 1 else {}
                metadata[metadata_path] = {
                    "case_id": case_id, "prompt": prompts[prompt],
                    "ratio": ratio, "seed": str(seed), **shared_dimensions,
                    "models": [{"id": model, "label": models[model], **image_sizes[model],
                                "pixels": image_sizes[model]["width"] * image_sizes[model]["height"], **settings}
                               for model, settings in parameters.items()],
                }
                for reference_id, label in REFERENCE_MODELS.items():
                    reference = f"references/{case_id}/{reference_id}.png"
                    reference_path = directory / reference
                    if not reference_path.is_file() or not reference_path.resolve().is_relative_to(directory.resolve()):
                        continue
                    with Image.open(reference_path, formats=["PNG"]) as image:
                        reference_size = {"width": image.width, "height": image.height}
                    url = f"images/{case_id}/{reference_id}.png"
                    if static:
                        assets[url] = reference_path
                    else:
                        url = f"runs/{quote(name, safe='')}/{reference}"
                    images[reference_id] = url
                    image_previews[reference_id] = webp_paths(url)
                    image_sizes[reference_id] = reference_size
                    reference_metadata = f"metadata/{case_id}/{reference_id}.json"
                    image_metadata[reference_id] = reference_metadata
                    metadata[reference_metadata] = {
                        "case_id": case_id, "prompt": prompts[prompt], "ratio": ratio, "seed": None,
                        **reference_size, "pixels": reference_size["width"] * reference_size["height"],
                        "model": {"id": reference_id, "label": label},
                        "reference": True, "source": "user_provided",
                    }
                cases.append({
                    "id": case_id, "prompt": prompts[prompt],
                    "translation": translation if isinstance(translation, str) else None,
                    "ratio": ratio, "width": width, "height": height, "seed": str(seed),
                    "images": images, "image_previews": image_previews,
                    "image_sizes": image_sizes, "image_metadata": image_metadata,
                    "metadata": metadata_path,
                })
    return {
        "models": [
            *({"id": model, "label": label} for model, label in models.items()),
            *({"id": model, "label": label, "reference": True}
              for model, label in REFERENCE_MODELS.items()),
        ],
        "cases": cases,
    }, assets, metadata


def clean_pixels(image):
    pixels = image.convert("RGBA" if image.has_transparency_data else "RGB")
    pixels.info.clear()
    return pixels


def save_webp(pixels, target, variant):
    from PIL import Image

    _, long_edge, quality = WEBP_VARIANTS[variant]
    with pixels.copy() as resized:
        resized.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
        resized.save(target, format="WEBP", quality=quality, method=6)


@lru_cache(maxsize=128)
def local_webp(path, modified_ns, variant):
    from PIL import Image

    with Image.open(path) as image, clean_pixels(image) as pixels, BytesIO() as output:
        save_webp(pixels, output, variant)
        return output.getvalue()


def export_site(destination):
    from PIL import Image

    catalog, assets, metadata = public_catalog(static=True)
    destination.mkdir(parents=True, exist_ok=False)
    for name, _ in set(STATIC.values()):
        shutil.copyfile(ROOT / name, destination / name)
    for relative, source in assets.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Save only image pixels. ComfyUI embeds workflow and prompt metadata in PNGs.
        with Image.open(source) as image, clean_pixels(image) as pixels:
            pixels.save(target, compress_level=3)
            for variant, path in webp_paths(relative).items():
                save_webp(pixels, destination / path, variant)
    for relative, record in metadata.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "results.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"静态站点：{destination.resolve()}（{len(assets)} 张）")


class Viewer(BaseHTTPRequestHandler):
    def log_request(self, code="-", size="-"):
        if isinstance(code, int) and code >= 400:
            super().log_request(code, size)

    def respond(self, status, mime, size, *, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename is not None:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()

    def send_json(self, data, status=200, *, filename=None):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.respond(status, "application/json; charset=utf-8", len(body), filename=filename)
        self.wfile.write(body)

    def send_file(self, path, mime):
        with path.open("rb") as file:
            self.respond(200, mime, path.stat().st_size)
            shutil.copyfileobj(file, self.wfile)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        try:
            self.route(path)
        except FileNotFoundError:
            self.send_json({"error": "文件或批次不存在。"}, 404)
        except (ConnectionError, ConnectionAbortedError):
            pass
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.log_error("%s", error)
            self.send_json({"error": "无法读取本地 benchmark 产物，请检查终端日志。"}, 500)

    def route(self, path):
        if path in STATIC:
            file, mime = STATIC[path]
            self.send_file(ROOT / file, mime)
            return
        if path == "/results.json":
            catalog, _, _ = public_catalog()
            self.send_json(catalog)
            return
        if re.fullmatch(r"/metadata/[a-f0-9]{20}(?:/[\w-]+)?\.json", path, re.ASCII):
            _, _, metadata = public_catalog()
            if record := metadata.get(path.removeprefix("/")):
                model = record.get("model")
                suffix = f"-{model['id']}" if model else ""
                self.send_json(record, filename=f"benchmark-{record['case_id']}{suffix}.json")
                return
        if path.startswith("/runs/"):
            name, _, relative = path.removeprefix("/runs/").partition("/")
            variant = None
            for label, (suffix, _, _) in WEBP_VARIANTS.items():
                if relative.endswith(suffix):
                    relative = relative.removesuffix(suffix) + ".png"
                    variant = label
                    break
            if name in run_dirs() and (IMAGE.fullmatch(relative) or REFERENCE_IMAGE.fullmatch(relative)):
                directory = (RUNS / name).resolve()
                file = (directory / relative).resolve()
                if file.is_relative_to(directory):
                    if variant is None:
                        self.send_file(file, "image/png")
                    else:
                        body = local_webp(file, file.stat().st_mtime_ns, variant)
                        self.respond(200, "image/webp", len(body))
                        self.wfile.write(body)
                    return
        raise FileNotFoundError(path)


class ViewerServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    parser = argparse.ArgumentParser(description="本地 Benchmark 对比看板")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--export-dir", type=Path, help="将公开对比站点导出到一个新目录")
    args = parser.parse_args()
    if args.export_dir is not None:
        export_site(args.export_dir)
        return
    with open_viewer(args.port) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


@contextmanager
def open_viewer(port):
    try:
        server = ViewerServer(("127.0.0.1", port), Viewer)
    except OSError as error:
        if error.errno != errno.EADDRINUSE and getattr(error, "winerror", None) not in (10013, 10048):
            raise
        print(f"看板端口 {port} 已占用，自动选择可用端口。", flush=True)
        server = ViewerServer(("127.0.0.1", 0), Viewer)
    with server:
        print(f"Benchmark 看板 http://127.0.0.1:{server.server_port}/", flush=True)
        yield server


@contextmanager
def serve_in_background(server):
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield
    finally:
        server.shutdown()
        worker.join()


if __name__ == "__main__":
    main()
