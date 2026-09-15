"""
Second Person 启动入口（产品文档 §运行方式 / 开发文档 §6.15 启动流程）。

python start.py                 一键启动，浏览器自动打开
python start.py --port 8001     指定端口
python start.py --data-dir D:/second-person-data  指定运行数据目录
python start.py stop            停止主程序（默认保留常驻 Embedding）
python start.py stop --all      停止主程序 + 常驻 Embedding
python start.py restart         软重启（复用 Embedding，避免重载 BGE-M3）
python start.py --rebuild-index 从 md 重建 SQLite 索引后退出
python start.py --recompile     从 raw_docs+conversations 重建记忆 md 后退出

启动优化（快路径）：
- Embedding：先听端口再后台加载模型；wait=False 不堵主程序；persist 跨 stop 复用
- ComfyUI：默认与主程序一起后台拉起（wait=false 不堵）；config 可设 lazy:true 改为按需
- AppContainer：核心就绪即可对话；IM/连接器/自检后台加载
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
PID_FILE = Path.home() / ".second-person" / "gateway.pid"
PORT_FILE = Path.home() / ".second-person" / "gateway.port"
EMBEDDING_PID_FILE = Path.home() / ".second-person" / "embedding.pid"

# ---- 本地 Embedding 服务（embedding/serve.py，跑在隔离的 embedding/venv） ----
EMBEDDING_PORT = 8100
EMBEDDING_HEALTH = f"http://127.0.0.1:{EMBEDDING_PORT}/health"
EMBEDDING_SERVE = BASE_DIR / "embedding" / "serve.py"
EMBEDDING_VENV_PY = (BASE_DIR / "embedding" / "venv" / "Scripts" / "python.exe"
                     if sys.platform.startswith("win")
                     else BASE_DIR / "embedding" / "venv" / "bin" / "python")


def _find_port(preferred: int) -> int:
    candidates = [preferred] + list(range(8001, 8011))
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError(
        f"端口 {preferred} 及 8001-8010 均被占用，请用 --port 指定")


def _wait_port_free(port: int, timeout: float = 10.0) -> bool:
    """等待端口释放（restart 停后重启前调用），避免强杀未释放造成端口漂移。"""
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return True
        _t.sleep(0.3)
    return False


# ============================================================
# 统一服务编排（方案 A）：按依赖顺序拉起内部子进程与外部服务，
# 就绪探测后再启主程序，退出时逆序统一终止。外部服务一律 optional，
# 失败不阻断核心应用；已在运行（就绪探测通过）则跳过启动与接管。
# ============================================================
@dataclass
class ServiceSpec:
    name: str
    command: object                       # str（shell 执行）| list[str]（直接执行）
    cwd: str | None = None
    enabled: bool = True
    optional: bool = True                 # True：失败仅告警不阻断
    depends_on: list = field(default_factory=list)
    ready: dict = field(default_factory=dict)   # {"type":"port"/"http", ...}
    ready_timeout: int = 60
    wait: bool = True                     # False：启动后不阻塞等就绪（后台起）
    env_file: str | None = None           # KEY=VALUE 文件，注入子进程环境
    persist: bool = False                 # True：主进程退出/普通 stop 不杀
    lazy: bool = False                    # True：冷启动不拉起，按需 ensure


def _parse_env_file(path: str) -> dict[str, str]:
    p = Path(path) if os.path.isabs(path) else BASE_DIR / path
    out: dict[str, str] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2)
    return out


class ServiceSupervisor:
    """轻量进程监督：拓扑启动 + 就绪探测 + 统一逆序终止。
    子服务脱离控制台启动（防父控制台信号误杀），中途退出不自动重拉。"""

    def __init__(self, specs: list[ServiceSpec]):
        self.specs = {s.name: s for s in specs}
        self._spawned: list[tuple[str, subprocess.Popen]] = []

    def _order(self) -> list[str]:
        seen: set[str] = set()
        order: list[str] = []

        def visit(n: str, stack: set[str]) -> None:
            if n in seen or n in stack or n not in self.specs:
                return
            stack.add(n)
            for dep in self.specs[n].depends_on:
                visit(dep, stack)
            stack.discard(n)
            seen.add(n)
            order.append(n)

        for name in self.specs:
            visit(name, set())
        return order

    def _ready(self, spec: ServiceSpec) -> bool:
        r = spec.ready or {}
        t = r.get("type")
        if t == "port":
            return _port_open(int(r.get("port", 0)), r.get("host", "127.0.0.1"))
        if t == "http":
            try:
                with urllib.request.urlopen(r["url"], timeout=3) as resp:
                    code = getattr(resp, "status", 200)
                    if not (200 <= code < 500):
                        return False
                    # require_status=ready：端口已听但模型仍在加载时不算就绪
                    want = r.get("require_status")
                    if want:
                        raw = resp.read().decode("utf-8", errors="ignore")
                        try:
                            import json as _json
                            body = _json.loads(raw or "{}")
                        except ValueError:
                            return False
                        return body.get("status") == want
                    return True
            except urllib.error.HTTPError:
                return True   # 连上了（即使 4xx/5xx）即视为已启动
            except Exception:  # noqa: BLE001
                return False
        return True

    def _wait_ready(self, spec: ServiceSpec) -> bool:
        deadline = time.time() + spec.ready_timeout
        while time.time() < deadline:
            if self._ready(spec):
                return True
            time.sleep(1)
        return False

    def start_all(self) -> None:
        for name in self._order():
            spec = self.specs[name]
            if not spec.enabled or not spec.command:
                continue
            if spec.lazy:
                # 冷启动跳过；首次使用时由 infrastructure.lazy_services.ensure_service 拉起
                if spec.ready and self._ready(spec):
                    print(f"[services] {name} 已在运行（lazy，跳过拉起）")
                else:
                    print(f"[services] {name} 懒启动：冷启动跳过，按需再拉")
                continue
            # persist 服务：端口已听即可跳过（embedding 允许 status=loading）
            if spec.ready and self._alive_enough(spec):
                print(f"[services] {name} 已在运行，跳过启动")
                continue
            proc = self._spawn(spec)
            if proc is None:
                continue
            self._spawned.append((name, proc))
            if spec.name == "embedding":
                _write_embedding_pid(proc.pid)
            if not spec.ready or not spec.wait:
                if proc.poll() is None and not spec.wait:
                    print(f"[services] {name} 已后台启动（不阻塞等就绪）")
                continue
            if self._wait_ready(spec):
                print(f"[services] {name} 已就绪")
            elif proc.poll() is not None:
                print(f"[services] {name} 进程已退出（可选服务，继续）"
                      if spec.optional else f"[services] {name} 进程已退出")
            else:
                print(f"[services] {name} 就绪超时，后台继续启动")

    def _alive_enough(self, spec: ServiceSpec) -> bool:
        """跳过拉起的条件：完整 ready，或 persist 服务仅端口/HTTP 可达。"""
        if self._ready(spec):
            return True
        if not spec.persist:
            return False
        r = spec.ready or {}
        if r.get("type") == "port":
            return _port_open(int(r.get("port", 0)), r.get("host", "127.0.0.1"))
        if r.get("type") == "http":
            try:
                with urllib.request.urlopen(r["url"], timeout=2) as resp:
                    return 200 <= getattr(resp, "status", 200) < 500
            except urllib.error.HTTPError:
                return True
            except Exception:  # noqa: BLE001
                return False
        return False

    def _spawn(self, spec: ServiceSpec):
        env = os.environ.copy()
        if spec.env_file:
            env.update(_parse_env_file(spec.env_file))
        cwd = None
        if spec.cwd:
            cwd = spec.cwd if os.path.isabs(
                spec.cwd) else str(BASE_DIR / spec.cwd)
        # 脱离控制台启动：父进程控制台收到的 Ctrl+C/中断信号不再波及子服务；
        # 脱离后子服务无控制台可打印，输出统一写入 data/logs/<name>.log
        log_dir = DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        popen_kw: dict = {}
        if sys.platform.startswith("win"):
            # CREATE_NEW_PROCESS_GROUP：隔离 Ctrl+C，不波及子服务
            # CREATE_NO_WINDOW：禁止弹出控制台窗口
            # 注意：不能加 DETACHED_PROCESS，否则 Windows 会为子进程分配新的可见控制台窗口
            popen_kw["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW)
            # STARTUPINFO + SW_HIDE 作为双重保障，尤其确保 shell=True 时 cmd.exe
            # 的子进程也不会弹出窗口
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            popen_kw["startupinfo"] = si
        else:
            popen_kw["start_new_session"] = True
        try:
            print(
                f"[services] 启动 {spec.name} ...（日志 → data/logs/{spec.name}.log）")
            log_f = open(log_dir / f"{spec.name}.log", "ab")
            if isinstance(spec.command, str):
                return subprocess.Popen(spec.command, cwd=cwd, env=env, shell=True,
                                        stdin=subprocess.DEVNULL, stdout=log_f,
                                        stderr=subprocess.STDOUT, **popen_kw)
            return subprocess.Popen([str(x) for x in spec.command], cwd=cwd,
                                    env=env, stdin=subprocess.DEVNULL,
                                    stdout=log_f, stderr=subprocess.STDOUT,
                                    **popen_kw)
        except OSError as e:
            msg = f"[services] {spec.name} 启动失败：{e}"
            if spec.optional:
                print(msg + "（可选服务，跳过）")
                return None
            raise

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        try:
            if sys.platform.startswith("win"):
                # 杀掉整个进程树（如 pnpm 下的 node 子进程）
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, check=False)
            else:
                proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def stop_all(self, *, stop_persist: bool = False) -> None:
        """停止本轮拉起的子服务。persist 服务默认保留，供下次 start 复用。"""
        for name, proc in reversed(self._spawned):
            if proc.poll() is not None:
                continue
            spec = self.specs.get(name)
            if spec and spec.persist and not stop_persist:
                print(f"[services] {name} 常驻保留（pid={proc.pid}）")
                continue
            print(f"[services] 停止 {name} ...")
            self._kill_tree(proc)
            if name == "embedding":
                _clear_embedding_pid()


def _write_embedding_pid(pid: int) -> None:
    try:
        EMBEDDING_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        EMBEDDING_PID_FILE.write_text(str(pid), encoding="utf-8")
    except (PermissionError, OSError):
        pass


def _clear_embedding_pid() -> None:
    try:
        if EMBEDDING_PID_FILE.exists():
            EMBEDDING_PID_FILE.unlink()
    except OSError:
        pass


def _stop_embedding_persist() -> None:
    """停止常驻 Embedding（--stop-all / stop --all）。"""
    pid = None
    if EMBEDDING_PID_FILE.exists():
        try:
            pid = int(EMBEDDING_PID_FILE.read_text().strip())
        except (ValueError, OSError):
            pid = None
    if pid and _pid_alive(pid):
        print(f"[services] 停止常驻 embedding pid={pid} ...")
        try:
            if sys.platform.startswith("win"):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, check=False)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError) as e:
            print(f"[services] 停止 embedding 失败：{e}")
    elif _port_open(EMBEDDING_PORT):
        # 无 PID 文件但端口占用：尽力按端口查找（Windows 用 netstat 太重，仅告警）
        print(f"[services] embedding 端口 {EMBEDDING_PORT} 仍在监听，"
              "但无 PID 文件；请手动结束对应进程")
    _clear_embedding_pid()


def _build_service_specs(include_embedding: bool,
                         include_external: bool) -> list[ServiceSpec]:
    """组装待编排的服务：内置 Embedding（命令由 venv 计算）+ config.yaml 声明的外部服务。"""
    specs: list[ServiceSpec] = []
    if include_embedding and EMBEDDING_SERVE.exists() and EMBEDDING_VENV_PY.exists():
        specs.append(ServiceSpec(
            name="embedding",
            command=[str(EMBEDDING_VENV_PY), str(EMBEDDING_SERVE),
                     "--port", str(EMBEDDING_PORT)],
            # 先听后载：HTTP 可达即可跳过重复拉起；模型 ready 不堵主路径
            ready={"type": "http", "url": EMBEDDING_HEALTH,
                   "require_status": "ready"},
            ready_timeout=90,
            wait=False,
            optional=True,
            persist=True,
            lazy=False))
    if include_external:
        try:
            from infrastructure.config_manager import ConfigManager
            cfg = ConfigManager(DATA_DIR / "config.yaml")
            for name, s in (cfg.get_raw("services", {}) or {}).items():
                if not isinstance(s, dict):
                    continue
                # ComfyUI 可在 config 中设 lazy:true 改为按需拉起；默认与主程序一起启动
                default_lazy = False
                specs.append(ServiceSpec(
                    name=name, command=s.get("command"), cwd=s.get("cwd"),
                    enabled=s.get("enabled", True), optional=s.get("optional", True),
                    depends_on=s.get("depends_on", []) or [],
                    ready=s.get("ready", {}) or {},
                    ready_timeout=int(s.get("ready_timeout", 60)),
                    wait=s.get("wait", True),
                    env_file=s.get("env_file"),
                    persist=bool(s.get("persist", False)),
                    lazy=bool(s.get("lazy", default_lazy))))
        except Exception as e:  # noqa: BLE001
            print(f"[services] 读取外部服务配置失败，仅启内部服务：{e}")
    return specs


def _pid_alive(pid: int) -> bool:
    """进程是否存活（跨平台）。"""
    if sys.platform.startswith("win"):
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, check=False)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _single_instance_check(port: int) -> bool:
    """返回 True 表示已有本程序实例在运行（应直接打开页面并退出）。
    同时校验 PID 存活与端口监听：避免无关程序占用端口时误判为已有实例。"""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text())
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                port_open = s.connect_ex(("127.0.0.1", port)) == 0
            if port_open and _pid_alive(pid):
                return True
        except (ValueError, OSError):
            pass
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        PID_FILE.write_text(str(os.getpid()))
    except (PermissionError, OSError):
        pass  # sandbox/chroot 环境无法写 PID 文件，跳过
    return False


def _bootstrap_data():
    from infrastructure.bootstrap import init_data_dir
    init_data_dir(DATA_DIR)


async def _run_rebuild_index():
    from infrastructure.db import Database
    from memory.recovery import rebuild_index
    _bootstrap_data()
    db = Database(DATA_DIR / "palace.db")
    db.run_migrations(str(BASE_DIR / "migrations"))
    result = rebuild_index(db, DATA_DIR)
    print(f"--rebuild-index 完成：重建 {result['rebuilt']} 条")


async def _run_recompile():
    from app.container import AppContainer
    from memory.recovery import recompile
    _bootstrap_data()
    c = AppContainer(DATA_DIR)
    c.db.run_migrations(str(BASE_DIR / "migrations"))
    c.vs.load()
    await c.fw.start()
    await recompile(c.db, DATA_DIR, c.distiller, c.backup)
    await c.fw.stop()


async def _run_rededup():
    """离线全量回溯去重：清理提炼当刻 Embedding 不可用遗留的重复记忆。
    需在主服务停止时运行（避免双 FileWriter 并发写入）。"""
    from app.container import AppContainer
    _bootstrap_data()
    c = AppContainer(DATA_DIR)
    c.db.run_migrations(str(BASE_DIR / "migrations"))
    c.vs.load()
    await c.fw.start()
    result = await c.distiller.rededup_all()
    await c.fw.stop(drain_timeout=60)   # 等待合并/删除写入落盘
    print(f"--rededup 完成：扫描 {result['scanned']} 条，合并删除 {result['merged']} 条重复")


def _read_pid() -> int | None:
    if PID_FILE.exists():
        try:
            return int(PID_FILE.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) == 0


def _cmd_status(port: int) -> None:
    pid = _read_pid()
    # 优先用持久化的实际端口（可能因 fallback 与参数端口不同）
    if PORT_FILE.exists():
        try:
            port = int(PORT_FILE.read_text().strip())
        except (ValueError, OSError):
            pass
    running = _port_open(port)
    if running:
        print(f"Second Person 运行中（pid={pid or '?'}，端口 {port}）")
    elif pid:
        print(f"PID 文件存在（pid={pid}）但端口 {port} 未监听，可能已停止")
    else:
        print("Second Person 未运行")


def _cmd_stop(*, stop_all_services: bool = False) -> None:
    pid = _read_pid()
    if not pid:
        print("未找到运行中的实例（无 PID 文件）")
        if stop_all_services:
            _stop_embedding_persist()
        return
    try:
        if sys.platform.startswith("win"):
            if stop_all_services:
                # /T 终止整棵进程树（含本轮非 persist 子服务）
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, check=False)
            else:
                # 只杀主进程，保留 persist embedding / 外部已托管服务
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, check=False)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
        print(f"已发送停止信号给 pid={pid}"
              + ("（全量）" if stop_all_services else "（保留常驻 Embedding）"))
    except (ProcessLookupError, OSError) as e:
        print(f"停止失败或进程已退出：{e}")
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()
        if stop_all_services:
            _stop_embedding_persist()


def _cmd_install_service(port: int) -> None:
    """生成平台对应的常驻服务安装配置并打印说明。"""
    py = sys.executable
    script = str(BASE_DIR / "start.py")
    if sys.platform.startswith("linux"):
        unit = ("[Unit]\nDescription=Second Person\nAfter=network.target\n\n"
                "[Service]\n"
                f"ExecStart={py} {script} start --no-browser --port {port}\n"
                "Restart=on-failure\n\n[Install]\nWantedBy=default.target\n")
        out = Path.home() / ".config" / "systemd" / "user" / "second-person.service"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(unit, encoding="utf-8")
        print(f"已生成 systemd user unit：{out}")
        print("启用：systemctl --user daemon-reload && "
              "systemctl --user enable --now second-person")
    elif sys.platform == "darwin":
        plist = ("<?xml version=\"1.0\"?>\n<plist version=\"1.0\"><dict>\n"
                 "<key>Label</key><string>com.secondperson</string>\n"
                 "<key>ProgramArguments</key><array>"
                 f"<string>{py}</string><string>{script}</string>"
                 f"<string>start</string><string>--no-browser</string>"
                 f"<string>--port</string><string>{port}</string></array>\n"
                 "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>\n"
                 "</dict></plist>\n")
        out = Path.home() / "Library" / "LaunchAgents" / "com.secondperson.plist"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(plist, encoding="utf-8")
        print(f"已生成 launchd plist：{out}")
        print(f"启用：launchctl load {out}")
    elif sys.platform.startswith("win"):
        cmd = (f'schtasks /Create /SC ONLOGON /TN SecondPerson /TR '
               f'"\\"{py}\\" \\"{script}\\" start --no-browser --port {port}" /F')
        print("在管理员 PowerShell 中执行以下命令注册开机自启：")
        print(cmd)
    else:
        print("未知平台，请手动配置常驻服务")


def main():
    global DATA_DIR
    parser = argparse.ArgumentParser(description="Second Person")
    parser.add_argument("command", nargs="?", default="start",
                        choices=["start", "stop", "status", "restart",
                                 "install-service"],
                        help="服务命令（默认 start）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path,
                        help="运行数据目录，默认项目下 data/")
    parser.add_argument("--rebuild-index", action="store_true")
    parser.add_argument("--recompile", action="store_true")
    parser.add_argument("--rededup", action="store_true",
                        help="全量回溯去重：合并 Embedding 不可用期遗留的重复记忆后退出")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-embedding", action="store_true",
                        help="不拉起本地 Embedding 服务（检索降级 FTS5）")
    parser.add_argument("--no-services", action="store_true",
                        help="不拉起 config.yaml 声明的外部服务（PostgreSQL/Langfuse 等）")
    parser.add_argument("--all", action="store_true",
                        help="配合 stop/restart：同时停止常驻 Embedding 等 persist 服务")
    args = parser.parse_args()

    if args.data_dir is not None:
        DATA_DIR = args.data_dir.resolve()

    _bootstrap_data()

    # 服务管理子命令
    if args.command == "status":
        _cmd_status(args.port)
        return
    if args.command == "stop":
        _cmd_stop(stop_all_services=bool(args.all))
        return
    if args.command == "restart":
        # 重启前优先复用上一实例的实际端口，避免熔断/fallback 造成端口漂移
        _prev_port = None
        if PORT_FILE.exists():
            try:
                _prev_port = int(PORT_FILE.read_text().strip())
            except (ValueError, OSError):
                _prev_port = None
        # 默认软重启：保留 Embedding 常驻，避免每次重载 BGE-M3
        _cmd_stop(stop_all_services=bool(args.all))
        # 等待端口真正释放（强杀后 Windows 端口未必立即释放）
        _wait_port_free(_prev_port or args.port, timeout=10.0)
        if _prev_port:
            args.port = _prev_port
        # 继续走 start 流程
    if args.command == "install-service":
        _cmd_install_service(args.port)
        return

    if args.rebuild_index:
        asyncio.run(_run_rebuild_index())
        return
    if args.recompile:
        asyncio.run(_run_recompile())
        return
    if args.rededup:
        asyncio.run(_run_rededup())
        return

    from infrastructure.startup_timing import StartupTimer
    timer = StartupTimer()

    # md schema 迁移
    from memory.md_schema import run_md_migrations
    run_md_migrations(DATA_DIR, BASE_DIR / "md_migrations")
    timer.mark("md_migrations")

    port = _find_port(args.port)
    if _single_instance_check(port):
        print(f"检测到已有实例在 {port} 运行，打开页面")
        if not args.no_browser:
            webbrowser.open(f"http://localhost:{port}")
        return
    # 持久化实际监听端口：供 restart 复用、status 准确探测（避免 fallback 后误报）
    try:
        PORT_FILE.write_text(str(port))
    except (PermissionError, OSError):
        pass

    import uvicorn
    from app.main import create_app
    app = create_app(DATA_DIR)
    timer.mark("create_app")

    # 统一服务编排：Embedding 后台/常驻，不阻塞主程序；ComfyUI 默认懒启动
    supervisor = ServiceSupervisor(_build_service_specs(
        include_embedding=not args.no_embedding,
        include_external=not args.no_services))
    supervisor.start_all()
    timer.mark("services_started")

    if not args.no_browser:
        def _open_browser_when_ready():
            """后台轮询服务端口，HTTP 可响应后再打开浏览器，最长等待 30 秒。"""
            url = f"http://localhost:{port}"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    urllib.request.urlopen(url, timeout=2)
                    webbrowser.open(url)
                    return
                except Exception:
                    time.sleep(0.5)
            # 超时兜底：仍然尝试打开，让用户看到浏览器反馈
            webbrowser.open(url)

        threading.Thread(target=_open_browser_when_ready, daemon=True).start()

    print(f"Second Person 启动于 http://localhost:{port}")
    try:
        timer.dump(DATA_DIR / "logs" / "startup-timing.json")
    except Exception:  # noqa: BLE001
        pass
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    finally:
        # 默认保留 persist Embedding，加速下次启动
        supervisor.stop_all(stop_persist=False)
        if PID_FILE.exists():
            PID_FILE.unlink()


if __name__ == "__main__":
    main()
