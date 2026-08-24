"""Prompt 注册对账测试（docs/PROMPT_REGISTRY.md ↔ 代码引用 ↔ prompts/ 目录）。

保护契约：
1. 正向对账：代码中所有 PROMPTS.load_raw/render 引用的 md 文件必须存在
   （把惰性加载 prompt 的"线上首次调用才炸"提前到测试阶段）
2. 反向对账：三个 prompts/ 目录下的每个 md 必须被代码引用（防死文件堆积）
3. 清单对账：docs/PROMPT_REGISTRY.md 注册表与实际 md 文件一一对应
4. 变量契约：md 内 ${var} 占位符与 render 调用传参完全一致；
   仅 load_raw 加载的 md 不得含占位符（safe_substitute 会静默保留导致漏渲染）
5. 调用点对账（含 C 类）：全库 llm.chat/stream/function_call 调用点与
   调用点注册表双向一致，新增 LLM 能力不登记即失败
6. 构成对账：调用点注册表“prompt 构成”列登记的 md 必须在同文件内被引用
运行：python tests/test_prompt_registry.py（退出码 0 = 全部通过）
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

# 扫描范围：全部后端包（新增包自动纳入：凡含 __init__.py 的顶层目录）
SCAN_DIRS = [d for d in ROOT.iterdir()
             if d.is_dir() and (d / "__init__.py").exists()]
# prompts 目录约定位置（新增模块的 prompts/ 目录需在此登记）
PROMPT_DIRS = ["agent/prompts", "app/prompts", "soul/prompts"]
REGISTRY_DOC = ROOT / "docs" / "PROMPT_REGISTRY.md"

# string.Template 的 ${var} / $var 两种占位符（项目约定只用 ${var}）
_PLACEHOLDER_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}"
                             r"|([A-Za-z_][A-Za-z0-9_]*))")


def collect_code_refs() -> dict[str, list[tuple[str, str, set | None]]]:
    """AST 扫描全部 .py，收集 PROMPTS.load_raw/render 引用。

    返回 {prompt名: [(文件, 方法, kwargs集合或None表示**动态展开)]}。
    """
    refs: dict[str, list[tuple[str, str, set | None]]] = {}
    for pkg in SCAN_DIRS:
        for py in pkg.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("load_raw", "render")
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "PROMPTS"):
                    continue
                if not (node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    raise AssertionError(
                        f"{py.relative_to(ROOT)}: PROMPTS.{node.func.attr} 的 "
                        f"prompt 名必须是字符串字面量（静态对账依赖此约定）")
                kwargs: set | None = set()
                for kw in node.keywords:
                    if kw.arg is None:  # **展开，无法静态解析
                        kwargs = None
                        break
                    kwargs.add(kw.arg)
                refs.setdefault(node.args[0].value, []).append(
                    (str(py.relative_to(ROOT)), node.func.attr, kwargs))
    return refs


def collect_md_names() -> set[str]:
    names = set()
    for d in PROMPT_DIRS:
        p = ROOT / d
        assert p.is_dir(), f"prompts 目录缺失：{d}"
        for f in p.glob("*.md"):
            names.add(f"{d}/{f.stem}")
    return names


def collect_registry_names() -> set[str]:
    assert REGISTRY_DOC.exists(), "注册清单 docs/PROMPT_REGISTRY.md 不存在"
    names = set()
    for ln in REGISTRY_DOC.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*\d+\s*\|\s*([\w/]+\.md)\s*\|", ln)
        if m:
            names.add(m.group(1)[:-3])  # 去掉 .md
    return names


def placeholders_of(name: str) -> set[str]:
    text = (ROOT / (name + ".md")).read_text(encoding="utf-8")
    return {a or b for a, b in _PLACEHOLDER_RE.findall(text)}


# LLM 调用点扫描：receiver 为 llm / *.llm（如 self.llm、c.llm）的三个方法
LLM_METHODS = ("chat", "stream", "stream_chat", "function_call")


def collect_llm_call_sites() -> dict[str, set[str]]:
    """AST 扫描全部 .py，收集 LLM 调用点。返回 {文件::函数: {方法}}。"""
    sites: dict[str, set[str]] = {}
    for pkg in SCAN_DIRS:
        for py in pkg.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            rel = str(py.relative_to(ROOT)).replace("\\", "/")

            def walk(node, func_stack):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func_stack = func_stack + [node.name]
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in LLM_METHODS):
                    r = node.func.value
                    if ((isinstance(r, ast.Name) and r.id == "llm")
                            or (isinstance(r, ast.Attribute) and r.attr == "llm")):
                        fn = func_stack[-1] if func_stack else "<module>"
                        sites.setdefault(f"{rel}::{fn}", set()).add(
                            node.func.attr)
                for ch in ast.iter_child_nodes(node):
                    walk(ch, func_stack)

            walk(tree, [])
    return sites


def collect_registry_call_sites() -> dict[str, tuple[str, set[str]]]:
    """解析调用点注册表。返回 {文件::函数: (方法, 构成列登记的md名集合)}。"""
    entries: dict[str, tuple[str, set[str]]] = {}
    for ln in REGISTRY_DOC.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*\d+\s*\|\s*([\w/.]+::\w+)\s*\|"
                     r"\s*(\w+)\s*\|[^|]*\|([^|]*)\|", ln)
        if m:
            mds = {p[:-3] for p in re.findall(r"[\w/]+\.md", m.group(3))}
            entries[m.group(1)] = (m.group(2), mds)
    return entries


def test_forward_refs_exist():
    """正向：代码引用的每个 prompt 必须有对应 md 文件。"""
    missing = [f"{n}（{files[0][0]}）" for n, files in collect_code_refs().items()
               if not (ROOT / (n + ".md")).exists()]
    assert not missing, f"代码引用了不存在的 prompt 文件：{missing}"


def test_reverse_no_orphan_md():
    """反向：prompts/ 目录下每个 md 必须被代码引用。"""
    orphans = collect_md_names() - set(collect_code_refs())
    assert not orphans, f"存在无代码引用的 prompt 死文件：{sorted(orphans)}"


def test_registry_doc_in_sync():
    """清单：PROMPT_REGISTRY.md 注册表与实际 md 文件一一对应。"""
    actual, registered = collect_md_names(), collect_registry_names()
    unregistered = actual - registered
    stale = registered - actual
    assert not unregistered, \
        f"以下 prompt 未登记到 docs/PROMPT_REGISTRY.md：{sorted(unregistered)}"
    assert not stale, \
        f"docs/PROMPT_REGISTRY.md 登记了已不存在的 prompt：{sorted(stale)}"


def test_variable_contract():
    """变量契约：${var} 占位符与 render 传参一致；load_raw 的 md 无占位符。"""
    problems = []
    for name, sites in collect_code_refs().items():
        if not (ROOT / (name + ".md")).exists():
            continue  # 存在性问题由正向对账用例报告
        vars_in_md = placeholders_of(name)
        for file, method, kwargs in sites:
            if method == "load_raw" and vars_in_md:
                problems.append(
                    f"{name} 含占位符 {sorted(vars_in_md)} 但 {file} 用 load_raw "
                    f"加载（不会渲染，占位符将原样输出）")
            elif method == "render":
                if kwargs is None:
                    continue  # **动态展开，跳过静态校验
                if kwargs != vars_in_md:
                    problems.append(
                        f"{name} 占位符 {sorted(vars_in_md)} 与 {file} 的 render "
                        f"传参 {sorted(kwargs)} 不一致")
    assert not problems, "变量契约不一致：\n" + "\n".join(problems)


def test_all_prompts_loadable():
    """全量可加载：经 PromptLoader 逐个 load_raw，内容非空。"""
    sys.path.insert(0, str(ROOT))
    from infrastructure.prompt_loader import PROMPTS
    for name in sorted(collect_md_names()):
        text = PROMPTS.load_raw(name)
        assert text.strip(), f"prompt 内容为空：{name}.md"


def test_call_sites_registered():
    """调用点对账：全库 LLM 调用点与注册表双向一致，方法名相符。"""
    actual = collect_llm_call_sites()
    registered = collect_registry_call_sites()
    unregistered = set(actual) - set(registered)
    stale = set(registered) - set(actual)
    assert not unregistered, \
        f"新增 LLM 调用点未登记到 PROMPT_REGISTRY.md：{sorted(unregistered)}"
    assert not stale, \
        f"PROMPT_REGISTRY.md 登记了已不存在的调用点：{sorted(stale)}"
    mismatch = [f"{k}：实际 {sorted(actual[k])} ≠ 登记 {registered[k][0]}"
                for k in actual if registered[k][0] not in actual[k]]
    assert not mismatch, "调用点方法名不一致：\n" + "\n".join(mismatch)


def test_call_site_md_composition():
    """构成对账：调用点注册表登记的 md 必须在同文件内被 PROMPTS 引用。"""
    refs_by_file: dict[str, set[str]] = {}
    for name, sites in collect_code_refs().items():
        for file, _method, _kw in sites:
            refs_by_file.setdefault(file.replace("\\", "/"), set()).add(name)
    problems = []
    for site, (_method, mds) in collect_registry_call_sites().items():
        file = site.split("::")[0]
        missing = mds - refs_by_file.get(file, set())
        if missing:
            problems.append(f"{site} 登记了 {sorted(missing)}，但 {file} 内"
                            f"无对应 PROMPTS 引用")
    assert not problems, "调用点构成与实现脱节：\n" + "\n".join(problems)


if __name__ == "__main__":
    tests = [test_forward_refs_exist, test_reverse_no_orphan_md,
             test_registry_doc_in_sync, test_variable_contract,
             test_all_prompts_loadable, test_call_sites_registered,
             test_call_site_md_composition]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[OK] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
    sys.exit(1 if failed else 0)
