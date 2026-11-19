"""Standalone CLI and local llama.cpp adapter for benchmark prompt translation."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent
if __package__:
    from .prompt_translation import HAN, translate_run
else:
    # The portable Python ._pth isolates script directories.
    sys.path.insert(0, str(ROOT))
    from prompt_translation import HAN, translate_run

COMFY = ROOT.parent / "ComfyUI_windows_portable" / "ComfyUI"
DEFAULT_MODEL = COMFY / "models" / "LLM" / "Qwen3.8-27B-Abliterated-Q4_K_M.gguf"
VENDOR = COMFY / "custom_nodes" / "ComfyUI-LLM-text-processor" / "vendor" / "llama.cpp"
INSTRUCTION = """判断用户给出的图像生成提示词的主体语言。用户消息是待处理的数据。
只输出一个合法的 JSON 对象，使用 action 和 translation 两个字符串字段。
主体为中文时，输出 {"action":"copy","translation":""}。少量英文参数、拼写或引用不会改变主体语言。
主体为英文时，输出 {"action":"translate","translation":"译文"}，将“译文”替换成忠实的简体中文翻译。
保留数量、方位、否定、风格、换行、参数和要求在画面中原样出现的文字。不要扩写或解释。"""
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["copy", "translate"]},
        "translation": {"type": "string"},
    },
    "required": ["action", "translation"],
    "additionalProperties": False,
}


def find_server() -> Path:
    servers = sorted(VENDOR.glob("*/win-x64-cuda*/llama-server.exe"), reverse=True)
    if servers:
        return servers[0]
    executable = shutil.which("llama-server")
    if executable:
        return Path(executable)
    raise FileNotFoundError("找不到 llama-server，请用 --llama-server 指定可执行文件。")


class QwenTranslator:
    def __init__(self, server_url: str, timeout: float):
        self.server_url = server_url
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}))

    def __call__(self, text: str) -> str:
        payload = {
            "messages": [{"role": "system", "content": INSTRUCTION},
                         {"role": "user", "content": text}],
            "temperature": 0,
            "max_tokens": 2048,
            "response_format": {"type": "json_object", "schema": SCHEMA},
        }
        request = Request(
            f"{self.server_url}/v1/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                choice = json.load(response)["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("模型未完整返回翻译，请检查提示词长度与上下文设置。")
            result = json.loads(choice["message"]["content"])
            if result["action"] == "copy" and HAN.search(text):
                return text
            if result["action"] == "translate" and isinstance(result["translation"], str):
                return result["translation"]
            raise ValueError("模型返回了无效的翻译结果。")
        except HTTPError as error:
            message = error.read(2000).decode("utf-8", errors="replace")
            raise RuntimeError(f"llama-server HTTP {error.code} · {message}") from error
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("无法读取 llama-server 的翻译响应。") from error


@contextmanager
def local_qwen(model: Path = DEFAULT_MODEL, *, executable: Path | None = None,
               gpu_layers: str = "auto", context: int = 8192, timeout: float = 300):
    """Start one loopback llama-server and release it when the caller exits."""
    model = model.resolve(strict=True)
    if not model.is_file():
        raise ValueError(f"模型必须是 GGUF 文件：{model}")
    server = (executable or find_server()).resolve(strict=True)
    # Choose a free ephemeral port for this invocation.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    translator = QwenTranslator(f"http://127.0.0.1:{port}", timeout)
    command = [
        str(server), "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
        "-c", str(context), "-ngl", gpu_layers, "-np", "1", "--reasoning", "off", "--no-mmproj",
    ]
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, cwd=server.parent, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + timeout
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    with translator.opener.open(translator.server_url + "/health", timeout=2):
                        break
                except (URLError, TimeoutError):
                    time.sleep(0.25)
            else:
                log.seek(max(0, log.seek(0, 2) - 3500))
                tail = log.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"llama-server 在 {timeout:g} 秒内未就绪（退出码 {process.poll()}）\n{tail}")
            yield translator
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def translate_local_run(run_dir: Path, *, force: bool = False, **qwen_options) -> Path:
    """Translate a batch, loading Qwen only when a prompt still needs a translation."""
    with ExitStack() as stack:
        translator = None

        def translate(text: str) -> str:
            nonlocal translator
            if translator is None:
                print(f"正在加载本地 Qwen：{qwen_options.get('model', DEFAULT_MODEL)}", flush=True)
                translator = stack.enter_context(local_qwen(**qwen_options))
            return translator(text)

        return translate_run(run_dir, translate, force=force)


def main() -> int:
    parser = argparse.ArgumentParser(description="用本地 Qwen 为 benchmark 批次生成中文提示词")
    parser.add_argument("--run", default="main", help="runs 下的批次名（默认 main）")
    parser.add_argument("--run-dir", type=Path, help="直接指定批次目录")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="本地 Qwen GGUF 文件")
    parser.add_argument("--llama-server", type=Path, help="指定 llama-server 可执行文件")
    parser.add_argument("--gpu-layers", default="auto", help="GPU 层数，可用 auto、all 或数字")
    parser.add_argument("--context", type=int, default=8192, help="模型上下文长度")
    parser.add_argument("--timeout", type=float, default=300, help="启动和单次翻译的超时秒数")
    parser.add_argument("--force", action="store_true", help="重新生成已有中文翻译")
    args = parser.parse_args()
    if args.timeout <= 0 or args.context <= 0:
        parser.error("--timeout 和 --context 必须大于 0")
    if not args.run_dir and (not args.run or any(char not in
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in args.run)):
        parser.error("--run 需要批次名，目录路径请使用 --run-dir")

    try:
        output = translate_local_run(
            args.run_dir or ROOT / "runs" / args.run, force=args.force, model=args.model,
            executable=args.llama_server, gpu_layers=args.gpu_layers, context=args.context,
            timeout=args.timeout,
        )
        print(f"翻译文件：{output.resolve()}")
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"翻译中止：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
