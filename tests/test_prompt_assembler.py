from agent.prompt_assembler import (
    PromptAssembler, PromptBlock, SessionCtx, ToolPromptBuilder,
)


def test_dynamic_blocks_are_always_last():
    assembler = PromptAssembler()
    blocks = [
        PromptBlock("dynamic-late", "D", 99, True),
        PromptBlock("response", "R", 20),
        PromptBlock("runtime", "S", 0),
        PromptBlock("dynamic-early", "E", 90, True),
    ]
    assert assembler.block_keys(blocks) == [
        "runtime", "response", "dynamic-early", "dynamic-late"
    ]
    prompt = assembler.assemble(blocks)
    assert prompt.index("runtime") < prompt.index("response")
    assert prompt.index("response") < prompt.index("dynamic-early")
    assert prompt.index("dynamic-late") > prompt.index("dynamic-early")


def test_empty_blocks_do_not_create_prompt_noise():
    prompt = PromptAssembler().assemble([
        PromptBlock("empty", "", 0), PromptBlock("used", "ok", 10)
    ])
    assert "empty" not in prompt
    assert "used" in prompt


class _Config:
    def get(self, key, default=None):
        return default


class _Spec:
    def __init__(self, name):
        self.name = name


class _Registry:
    """Full-catalog fake matching the denylist-based ToolRegistry API."""

    def __init__(self, names):
        self.specs = [_Spec(n) for n in names]

    def all_specs(self):
        return self.specs

    def openai_schemas(self):
        return [{"function": {"name": spec.name}} for spec in self.specs]

    def openai_schemas_excluding(self, denied):
        denied = denied or set()
        return [{"function": {"name": spec.name}}
                for spec in self.specs if spec.name not in denied]


ALL_TOOLS = [
    "web_search", "web_fetch", "memory_search", "memory_save",
    "fs_read", "fs_list", "fs_glob", "fs_grep", "fs_write", "fs_edit",
    "shell_exec", "datetime_now",
]


def _names(schemas):
    return sorted(item["function"]["name"] for item in schemas)


def test_tool_rules_have_no_projection_side_effects():
    builder = ToolPromptBuilder(_Registry(ALL_TOOLS), _Config())
    assert "工具由宿主" in builder.build_rules()
    assert "不要为了凑步骤调用工具" in builder.build_rules()


# --------------- 三档 × 项目/非项目 一致性（v6 沙箱统一化）--------------

def test_workspace_write_default_exposes_read_and_write():
    """默认档位（workspace-write）：fs_* 读写都在，shell 拒。"""
    builder = ToolPromptBuilder(_Registry(ALL_TOOLS), _Config())
    names = _names(builder.schemas(SessionCtx(sandbox_mode="workspace-write")))
    assert "fs_read" in names
    assert "fs_list" in names
    assert "fs_write" in names
    assert "fs_edit" in names
    assert "shell_exec" not in names
    # 非 fs 工具无条件常驻
    assert "web_search" in names
    assert "memory_save" in names


def test_readonly_mode_denies_write_and_shell():
    """read-only：fs 读 OK，写 + shell 全拒。"""
    builder = ToolPromptBuilder(_Registry(ALL_TOOLS), _Config())
    names = _names(builder.schemas(SessionCtx(sandbox_mode="read-only")))
    assert "fs_read" in names
    assert "fs_list" in names
    assert "fs_write" not in names
    assert "fs_edit" not in names
    assert "shell_exec" not in names


def test_danger_mode_opens_shell():
    """danger-full-access：读写 + shell 全开。"""
    builder = ToolPromptBuilder(_Registry(ALL_TOOLS), _Config())
    names = _names(builder.schemas(SessionCtx(sandbox_mode="danger-full-access")))
    assert "shell_exec" in names
    assert "fs_write" in names
    assert "fs_edit" in names
    assert "fs_read" in names


def test_default_sandbox_ctx_is_workspace_write():
    """SessionCtx 默认值就是 workspace-write（沙箱下沉后的合理默认）。"""
    ctx = SessionCtx()
    assert ctx.sandbox_mode == "workspace-write"


def test_schemas_byte_stable_across_calls():
    """核心不变式：同 session_ctx 出的 schemas 每次字节完全一致。"""
    builder = ToolPromptBuilder(_Registry(ALL_TOOLS), _Config())
    ctx = SessionCtx(sandbox_mode="workspace-write")
    first = builder.schemas(ctx)
    for _ in range(3):
        assert builder.schemas(ctx) == first


def test_empty_registry_returns_empty():
    builder = ToolPromptBuilder(_Registry([]), _Config())
    assert builder.schemas(SessionCtx()) == []
