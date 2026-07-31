"""
插件扩展机制（产品文档 §插件扩展机制）。

三条扩展路径：工具注册表 / LLM Provider / EventBus
- 工具插件：加载后自动注册到工具注册表
- Provider 插件：新增 provider_type 实现
- manifest.json：元数据和配置声明
- 生命周期钩子：on_load / on_unload
插件目录：plugins/{name}/（含 manifest.json + plugin.py）
"""
from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

logger = logging.getLogger("second_person.plugins")


class PluginManager:
    def __init__(self, plugins_dir, *, tool_registry, event_bus):
        self.plugins_dir = Path(plugins_dir)
        self.tool_registry = tool_registry
        self.event_bus = event_bus
        self._loaded: dict[str, object] = {}

    def discover_and_load(self) -> list[str]:
        loaded = []
        if not self.plugins_dir.exists():
            return loaded
        for d in self.plugins_dir.iterdir():
            if not d.is_dir():
                continue
            manifest = d / "manifest.json"
            entry = d / "plugin.py"
            if not manifest.exists() or not entry.exists():
                continue
            try:
                meta = json.loads(manifest.read_text(encoding="utf-8"))
                self._load_one(d.name, entry, meta)
                loaded.append(d.name)
            except Exception:  # noqa: BLE001
                logger.exception("插件加载失败：%s", d.name)
        return loaded

    def _load_one(self, name: str, entry: Path, meta: dict) -> None:
        spec = importlib.util.spec_from_file_location(
            f"sp_plugin_{name}", entry)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        on_load = getattr(mod, "on_load", None)
        if on_load:
            on_load(tool_registry=self.tool_registry, event_bus=self.event_bus,
                    manifest=meta)
        self._loaded[name] = mod
        logger.info("插件已加载：%s v%s", name, meta.get("version", "?"))

    def unload(self, name: str) -> None:
        mod = self._loaded.pop(name, None)
        if mod:
            on_unload = getattr(mod, "on_unload", None)
            if on_unload:
                on_unload()
