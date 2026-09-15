"""按需拉起 config.yaml 中标记为 lazy 的外部服务（如 ComfyUI）。

与 start.py 的 ServiceSupervisor 共用同一套 command/ready 约定，但独立于主进程
生命周期：主程序退出时不会杀掉这里拉起的常驻服务（除非调用 stop_lazy）。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("second_person.lazy_services")

_BASE = Path(__file__).resolve().parent.parent
_spawned: dict[str, subprocess.Popen] = {}


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) == 0


def _http_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return 200 <= getattr(resp, "status", 200) < 500
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001
        return False


def _is_ready(ready: dict[str, Any] | None) -> bool:
    if not ready:
        return True
    t = ready.get("type")
    if t == "port":
        return _port_open(int(ready.get("port", 0)), ready.get("host", "127.0.0.1"))
    if t == "http":
        return _http_ready(str(ready.get("url", "")))
    return True


def _parse_env_file(path: str) -> dict[str, str]:
    p = Path(path) if os.path.isabs(path) else _BASE / path
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _load_service_spec(name: str, data_dir: Path | None = None) -> dict[str, Any] | None:
    try:
        from infrastructure.config_manager import ConfigManager
        root = Path(data_dir) if data_dir is not None else (_BASE / "data")
        cfg = ConfigManager(root / "config.yaml")
        raw = (cfg.get_raw("services", {}) or {}).get(name)
        return raw if isinstance(raw, dict) else None
    except Exception:  # noqa: BLE001
        logger.debug("读取服务配置失败 name=%s", name, exc_info=True)
        return None


def ensure_service(name: str, *, timeout: float | None = None,
                   data_dir: Path | str | None = None) -> dict[str, Any]:
    """确保命名服务就绪；已在监听则直接返回。

    返回 {"ok": bool, "started": bool, "error": str|None}。
    """
    root = Path(data_dir) if data_dir is not None else None
    spec = _load_service_spec(name, root)
    if not spec:
        return {"ok": False, "started": False, "error": f"未配置服务 {name}"}
    if not spec.get("enabled", True):
        return {"ok": False, "started": False, "error": f"服务 {name} 已禁用"}

    ready = spec.get("ready") or {}
    if _is_ready(ready):
        return {"ok": True, "started": False, "error": None}

    if name in _spawned and _spawned[name].poll() is None:
        pass
    else:
        err = _spawn(name, spec)
        if err:
            return {"ok": False, "started": False, "error": err}

    limit = float(timeout if timeout is not None else spec.get("ready_timeout", 120))
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if _is_ready(ready):
            return {"ok": True, "started": True, "error": None}
        proc = _spawned.get(name)
        if proc is not None and proc.poll() is not None:
            return {"ok": False, "started": True,
                    "error": f"{name} 进程已退出 code={proc.returncode}"}
        time.sleep(0.5)
    return {"ok": False, "started": True, "error": f"{name} 就绪超时（{limit:.0f}s）"}


def _spawn(name: str, spec: dict[str, Any]) -> str | None:
    command = spec.get("command")
    if not command:
        return f"{name} 缺少 command"
    env = os.environ.copy()
    if spec.get("env_file"):
        env.update(_parse_env_file(str(spec["env_file"])))
    cwd = spec.get("cwd")
    if cwd and not os.path.isabs(str(cwd)):
        cwd = str(_BASE / cwd)
    log_dir = _BASE / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    popen_kw: dict = {}
    if sys.platform.startswith("win"):
        popen_kw["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        popen_kw["startupinfo"] = si
    else:
        popen_kw["start_new_session"] = True
    try:
        log_f = open(log_dir / f"{name}.log", "ab")
        if isinstance(command, str):
            proc = subprocess.Popen(
                command, cwd=cwd, env=env, shell=True,
                stdin=subprocess.DEVNULL, stdout=log_f, stderr=subprocess.STDOUT,
                **popen_kw)
        else:
            proc = subprocess.Popen(
                [str(x) for x in command], cwd=cwd, env=env,
                stdin=subprocess.DEVNULL, stdout=log_f, stderr=subprocess.STDOUT,
                **popen_kw)
        _spawned[name] = proc
        logger.info("按需启动服务 %s pid=%s", name, proc.pid)
        return None
    except OSError as exc:
        return str(exc)


async def ensure_comfyui(*, timeout: float = 180.0,
                         data_dir: Path | str | None = None) -> dict[str, Any]:
    """异步封装：供文生图/视频工具在本地后端未就绪时按需拉起。"""
    import asyncio
    return await asyncio.to_thread(
        ensure_service, "comfyui", timeout=timeout, data_dir=data_dir)
