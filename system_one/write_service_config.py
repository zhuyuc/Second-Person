"""Idempotent: write services.system_one into data/config.yaml."""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--exe", required=True, help="path to jev-style executable")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--release", default="0.8b")
    p.add_argument("--cpu", action="store_true",
                   help="Force CPU")
    p.add_argument("--cuda", action="store_true",
                   help="Force CUDA")
    p.add_argument("--lazy", action="store_true",
                   help="Cold-start skip; only ensure on first mood call")
    p.add_argument("--env-file", default="",
                   help="HF_HOME / HF_ENDPOINT env file (like embedding)")
    args = p.parse_args()

    from infrastructure.config_manager import ConfigManager

    exe = str(Path(args.exe).resolve())
    if args.cpu:
        device = "cpu"
    elif args.cuda:
        device = "cuda"
    else:
        device = "cuda" if shutil.which("nvidia-smi") else "cpu"

    cmd = [
        exe, "serve",
        "--backend", "torch",
        "--port", str(args.port),
        "--release", args.release,
        "--device", device,
    ]

    env_file = args.env_file.strip()
    if not env_file:
        env_file = str((ROOT / "system_one" / "hf.env").resolve())

    # 默认与主程序一起启动（同 embedding）；--lazy 才改为按需
    lazy = bool(args.lazy)

    cfg = ConfigManager(ROOT / "data" / "config.yaml")
    services = dict(cfg.get_raw("services", {}) or {})
    services["system_one"] = {
        "enabled": True,
        "optional": True,
        "persist": True,
        "lazy": lazy,
        "wait": False,
        "ready_timeout": 180,
        "ready": {
            "type": "http",
            "url": f"http://127.0.0.1:{args.port}/v1/models",
        },
        "env_file": env_file,
        "command": cmd,
    }
    cfg.set_raw("services", services)
    cfg.set_raw("mood_fast_path_timeout_ms", 1500)
    print(f"wrote services.system_one port={args.port} release={args.release} "
          f"device={device} lazy={lazy}")
    print(f"env_file={env_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
