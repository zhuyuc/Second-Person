"""P4-A domain 别名归并 + P4-B entity 消歧位 + P4-G 文件名清理。"""
from memory.naming import entity_id, memory_filename, normalize_domain


def test_domain_alias_collapse():
    for src, want in (
        ("product_design", "product"),
        ("product_management", "product"),
        ("pm", "product"),
        ("产品设计", "product"),
        ("frontend_architecture", "frontend"),
        ("web_development", "frontend"),
        ("前端开发", "frontend"),
        ("machine_learning", "ai"),
        ("LLM", "ai"),
        ("人工智能", "ai"),
        ("misc", "general"),
    ):
        assert normalize_domain(src) == want, f"{src} → {normalize_domain(src)}"


def test_domain_unknown_stays_as_is():
    # 未列在别名表的 domain 保持原样（走 general 归集由后台补）
    assert normalize_domain("bespoke_niche") == "bespoke_niche"


def test_entity_disambiguator_creates_distinct_ids():
    # 同名不同实体 → 不同 id
    a = entity_id("张三", "客户")
    b = entity_id("张三", "同事")
    same = entity_id("张三")
    assert a != b
    assert a != same
    assert same == entity_id("张三")  # 无消歧位向后兼容


def test_memory_filename_strips_emoji_and_symbols():
    name = memory_filename("mem_000001", "我 🎯 目标管理 / 计划")
    # 不出现 emoji / 斜杠 / 空格连续
    assert "🎯" not in name
    assert "/" not in name
    assert "__" not in name
    assert name.startswith("mem_000001_")
    assert name.endswith(".md")
