"""记事本工具仅 aside 频道可见。"""
from __future__ import annotations

from agent.prompt_assembler import SessionCtx, ToolPromptBuilder


class _Spec:
    def __init__(self, name, connector_id=None):
        self.name = name
        self.connector_id = connector_id


class _Reg:
    def __init__(self, names):
        self._specs = [_Spec(n) for n in names]

    def all_specs(self):
        return self._specs

    def openai_schemas_excluding(self, denied):
        denied = denied or set()
        return [{"function": {"name": s.name}} for s in self._specs if s.name not in denied]


class _Config:
    def get(self, *_a, **_k):
        return None


def test_notes_tools_only_on_aside():
    reg = _Reg(["fs_read", "notes_get", "notes_apply_edit", "notes_list", "shell_exec"])
    builder = ToolPromptBuilder(reg, _Config())
    main = builder.schemas(SessionCtx(sandbox_mode="danger-full-access", channel=None))
    names_main = {s["function"]["name"] for s in main}
    assert "notes_get" not in names_main
    assert "notes_apply_edit" not in names_main
    aside = builder.schemas(SessionCtx(sandbox_mode="danger-full-access", channel="aside"))
    names_aside = {s["function"]["name"] for s in aside}
    assert "notes_get" in names_aside
    assert "notes_apply_edit" in names_aside
