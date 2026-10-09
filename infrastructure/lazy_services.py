"""按需拉起 config.yaml 中标记为 lazy 的外部服务（如 ComfyUI / System One）。

与 start.py 的 ServiceSupervisor 共用同一套 command/ready 约定，但独立于主进程
生命周期：主程序退出时不会杀掉这里拉起的常驻服务（除非调用 stop_service）。

GPU 互斥：本地生图/生视频前可 release_gpu_for_media() 停掉 System One，
把 8GB 显存让给 ComfyUI；情绪识别下次调用再 ensure 拉起。
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
_SYSTEM_ONE_PID_FILE = Path.home() / ".second-person" / "system_one.pid"
_SYSTEM_ONE_PORT = 8765


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
        err = _spawn(name, spec, root)
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


def _augment_command(name: str, command: Any, data_dir: Path | None) -> Any:
    """ComfyUI：按 params 注入 --lowvram / --use-sage-attention 等无损启动参数。"""
    if name != "comfyui" or not command:
        return command
    try:
        from infrastructure.comfyui_accel.quality import (
            launch_extras_from_config,
            merge_launch_args,
        )
        from infrastructure.config_manager import ConfigManager
        root = Path(data_dir) if data_dir is not None else (_BASE / "data")
        cfg = ConfigManager(root / "config.yaml")
        extras = launch_extras_from_config(cfg)
        return merge_launch_args(command, extras)
    except Exception:  # noqa: BLE001
        logger.debug("ComfyUI 启动参数注入跳过", exc_info=True)
        return command


def _spawn(name: str, spec: dict[str, Any],
           data_dir: Path | None = None) -> str | None:
    command = _augment_command(name, spec.get("command"), data_dir)
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
        if name == "system_one":
            try:
                _SYSTEM_ONE_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
                _SYSTEM_ONE_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
            except OSError:
                logger.debug("写入 system_one pid 失败", exc_info=True)
        logger.info("按需启动服务 %s pid=%s", name, proc.pid)
        return None
    except OSError as exc:
        return str(exc)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError, PermissionError):
        return False
    except SystemError:
        # Windows：部分环境 os.kill(pid, 0) 行为不同，退回 OpenProcess 探测
        if sys.platform.startswith("win"):
            try:
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    return True
            except Exception:  # noqa: BLE001
                return False
        return False


def _kill_pid_tree(pid: int) -> None:
    if sys.platform.startswith("win"):
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, check=False)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass


def stop_service(name: str, *, data_dir: Path | str | None = None) -> dict[str, Any]:
    """停止按需/常驻子服务，释放端口与（若占用）GPU 显存。"""
    root = Path(data_dir) if data_dir is not None else None
    spec = _load_service_spec(name, root) or {}
    ready = spec.get("ready") or {}
    stopped = False

    proc = _spawned.pop(name, None)
    if proc is not None and proc.poll() is None:
        try:
            _kill_pid_tree(proc.pid)
            stopped = True
        except Exception:  # noqa: BLE001
            logger.debug("stop_service kill spawned 失败 name=%s", name, exc_info=True)

    if name == "system_one":
        pid = None
        if _SYSTEM_ONE_PID_FILE.exists():
            try:
                pid = int(_SYSTEM_ONE_PID_FILE.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pid = None
        if pid and _pid_alive(pid):
            _kill_pid_tree(pid)
            stopped = True
        try:
            if _SYSTEM_ONE_PID_FILE.exists():
                _SYSTEM_ONE_PID_FILE.unlink()
        except OSError:
            pass
        # 端口仍被占：尽量清掉监听进程（仅本机 loopback 服务）
        if _port_open(_SYSTEM_ONE_PORT):
            try:
                if sys.platform.startswith("win"):
                    out = subprocess.run(
                        ["netstat", "-ano"],
                        capture_output=True, text=True, check=False)
                    for line in (out.stdout or "").splitlines():
                        if f":{_SYSTEM_ONE_PORT}" in line and "LISTENING" in line:
                            parts = line.split()
                            owner = int(parts[-1])
                            if owner > 0:
                                _kill_pid_tree(owner)
                                stopped = True
            except Exception:  # noqa: BLE001
                logger.debug("stop_service 清端口失败", exc_info=True)

    # 等 ready 消失（最多几秒）
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and _is_ready(ready):
        time.sleep(0.25)
    still = _is_ready(ready) if ready else False
    logger.info("stop_service name=%s stopped=%s still_ready=%s",
                name, stopped, still)
    return {"ok": not still, "stopped": stopped, "still_ready": still}


def free_comfyui_vram(*, base_url: str = "http://127.0.0.1:8188") -> dict[str, Any]:
    """卸载 ComfyUI 空闲权重，给 Embedding / System One 腾显存（不杀进程）。"""
    import json
    import urllib.error
    import urllib.request

    root = (base_url or "").rstrip("/")
    if not root:
        return {"ok": False, "error": "empty base_url"}
    # 端口都没开就跳过
    try:
        from urllib.parse import urlparse
        host = urlparse(root).hostname or "127.0.0.1"
        port = urlparse(root).port or 8188
        if not _port_open(int(port), host):
            return {"ok": True, "skipped": True, "reason": "comfyui_down"}
    except Exception:  # noqa: BLE001
        pass
    payload = json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{root}/free", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return {"ok": 200 <= getattr(resp, "status", 200) < 500}
    except urllib.error.HTTPError as exc:
        # 部分版本无 /free：不阻断主链路
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


def release_gpu_for_media(*, data_dir: Path | str | None = None) -> dict[str, Any]:
    """本地生图/生视频前让出 GPU：停 System One（情绪模型按需再拉）。"""
    return stop_service("system_one", data_dir=data_dir)


def release_gpu_for_embedding(*, data_dir: Path | str | None = None) -> dict[str, Any]:
    """记忆检索向量化前尽量让出 GPU：只卸载 Comfy 空闲权重。

    不杀 System One（随主程序常驻的小模型）；大头是 ComfyUI 驻留权重。
    """
    del data_dir  # 接口保留，与 media 侧签名一致
    return {"comfyui": free_comfyui_vram()}


async def ensure_comfyui(*, timeout: float = 180.0,
                         data_dir: Path | str | None = None) -> dict[str, Any]:
    """异步封装：供文生图/视频工具在本地后端未就绪时按需拉起。"""
    import asyncio
    # 先释放情绪模型显存，再确保 ComfyUI
    await asyncio.to_thread(release_gpu_for_media, data_dir=data_dir)
    return await asyncio.to_thread(
        ensure_service, "comfyui", timeout=timeout, data_dir=data_dir)
