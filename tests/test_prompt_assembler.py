from agent.prompt_assembler import PromptAssembler, PromptBlock, ToolPromptBuilder


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
    def __init__(self):
        self.specs = [_Spec("web_search"), _Spec("calculator")]

    def all_specs(self):
        return self.specs

    def openai_schemas(self):
        return [{"function": {"name": spec.name}} for spec in self.specs]

    def openai_schemas_for(self, names):
        return [{"function": {"name": spec.name}}
                for spec in self.specs if names is None or spec.name in names]


def test_tool_projection_is_separate_from_tool_rules():
    builder = ToolPromptBuilder(_Registry(), _Config())
    assert "工具由宿主" in builder.build_rules()
    assert "不要为了凑步骤调用工具" in builder.build_rules()
    assert [item["function"]["name"] for item in builder.schemas("请搜索最新资料", 1)] == ["web_search"]


def test_tool_projection_does_not_expose_all_tools_for_normal_chat():
    builder = ToolPromptBuilder(_Registry(), _Config())
    assert builder.schemas("请解释一下 Agent Loop 是什么", 1) == []


def test_memory_save_requires_explicit_memory_instruction():
    registry = _Registry()
    registry.specs.append(_Spec("memory_save"))
    builder = ToolPromptBuilder(registry, _Config())
    assert [item["function"]["name"] for item in builder.schemas("记忆是什么", 1)] == []
    assert [item["function"]["name"] for item in builder.schemas("请记住我以后都用中文", 1)] == ["memory_save"]
