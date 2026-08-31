"""Coverage for agent.project_instructions baseline reconciliation."""
from __future__ import annotations

from pathlib import Path

from agent.project_instructions import (
    aggregate_hash, load_baseline, paths_hash_map, reconcile,
    DEFAULT_MAX_BYTES, TRUNCATION_MARKER,
)


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------- load ------

def test_load_baseline_missing_root(tmp_path: Path):
    files, truncated = load_baseline(tmp_path / "nope")
    assert files == ()
    assert truncated is False


def test_load_baseline_empty_project(tmp_path: Path):
    files, truncated = load_baseline(tmp_path)
    assert files == ()
    assert truncated is False


def test_load_baseline_reads_default_candidates_in_order(tmp_path: Path):
    _write(tmp_path, "README.md", "# Root readme\n\nHello.")
    _write(tmp_path, "AGENTS.md", "# Agents guide\n\nInstructions.")
    _write(tmp_path, "docs/README.md", "# Docs index\n\nSee sections.")
    files, truncated = load_baseline(tmp_path)
    assert truncated is False
    paths = [f.path for f in files]
    assert paths == ["README.md", "AGENTS.md", "docs/README.md"]
    # 每个文件独立 hash（内容不同）
    hashes = {f.hash for f in files}
    assert len(hashes) == 3


def test_load_baseline_deduplicates_identical_content(tmp_path: Path):
    body = "# Same body\n\n本项目描述"
    _write(tmp_path, "AGENTS.md", body)
    _write(tmp_path, "CLAUDE.md", body)
    files, _ = load_baseline(tmp_path)
    assert [f.path for f in files] == ["AGENTS.md"]


def test_load_baseline_skips_empty_files(tmp_path: Path):
    _write(tmp_path, "README.md", "   \n\n\n")
    _write(tmp_path, "AGENTS.md", "# Actual")
    files, _ = load_baseline(tmp_path)
    assert [f.path for f in files] == ["AGENTS.md"]


def test_load_baseline_per_file_truncation(tmp_path: Path):
    _write(tmp_path, "README.md", "A" * 40_000)
    files, _ = load_baseline(tmp_path, max_source_bytes=1000)
    assert len(files) == 1
    assert TRUNCATION_MARKER in files[0].content
    # 截断后仍有实质内容
    assert files[0].content.startswith("A")


def test_load_baseline_total_budget_stops_at_overflow(tmp_path: Path):
    _write(tmp_path, "README.md", "R" * 20_000)
    _write(tmp_path, "AGENTS.md", "A" * 20_000)
    _write(tmp_path, "CLAUDE.md", "C" * 20_000)
    files, truncated = load_baseline(
        tmp_path, max_bytes=25_000, max_source_bytes=20_000)
    # README 装下（20K + 少量 header），第二个（20K）就撑爆预算
    assert len(files) == 1
    assert files[0].path == "README.md"
    assert truncated is True


def test_load_baseline_rejects_symlink_escape(tmp_path: Path):
    """符号链接跳出项目根应被拒绝，防私密文件泄漏。"""
    outside = tmp_path.parent / "outside_secret.md"
    outside.write_text("SECRET: my api key", encoding="utf-8")
    root = tmp_path / "proj"
    root.mkdir()
    link = root / "README.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        # Windows without dev mode / no symlink privileges → skip
        return
    files, _ = load_baseline(root)
    for f in files:
        assert "SECRET" not in f.content


# --------------------------------------------------------- reconcile --------

def test_reconcile_initial_when_no_prior_state(tmp_path: Path):
    _write(tmp_path, "README.md", "# X")
    files, truncated = load_baseline(tmp_path)
    result = reconcile(None, None, files, truncated)
    assert result.kind == "initial"
    assert result.files_hash != ""
    assert "# X" in result.render_full()
    assert "workspace instructions" in result.render_full().lower()


def test_reconcile_unchanged_when_hashes_match(tmp_path: Path):
    _write(tmp_path, "README.md", "# X")
    files, truncated = load_baseline(tmp_path)
    agg = aggregate_hash(files)
    result = reconcile(agg, paths_hash_map(files), files, truncated)
    assert result.kind == "unchanged"
    # unchanged 不发内容，render_changes 应该是空串
    assert result.render_changes() == ""


def test_reconcile_changes_when_file_edited(tmp_path: Path):
    _write(tmp_path, "README.md", "# v1")
    files_v1, _ = load_baseline(tmp_path)
    old_hash = aggregate_hash(files_v1)
    old_paths = paths_hash_map(files_v1)
    # 用户改了 README
    _write(tmp_path, "README.md", "# v2 with updates")
    files_v2, truncated = load_baseline(tmp_path)
    result = reconcile(old_hash, old_paths, files_v2, truncated)
    assert result.kind == "changes"
    changes = result.render_changes()
    assert "updated" in changes.lower()
    assert "# v2" in changes


def test_reconcile_detects_removed_file(tmp_path: Path):
    _write(tmp_path, "README.md", "# root")
    _write(tmp_path, "AGENTS.md", "# agents")
    files_v1, _ = load_baseline(tmp_path)
    old_paths = paths_hash_map(files_v1)
    old_hash = aggregate_hash(files_v1)
    (tmp_path / "AGENTS.md").unlink()
    files_v2, _ = load_baseline(tmp_path)
    result = reconcile(old_hash, old_paths, files_v2, False)
    assert result.kind == "changes"
    assert "AGENTS.md" in result.removed
    assert "AGENTS.md" in result.render_changes()
    assert "no longer apply" in result.render_changes()


def test_reconcile_empty_when_project_has_no_instruction_files(tmp_path: Path):
    files, truncated = load_baseline(tmp_path)  # empty project
    result = reconcile("some-old-hash", {"README.md": "x"}, files, truncated)
    assert result.kind == "empty"
    # empty 状态不发任何内容（避免击穿）
    assert result.render_full() == ""
    assert result.render_changes() == ""


def test_aggregate_hash_stable_and_ordered():
    from agent.project_instructions import InstructionFile
    a = InstructionFile(path="A", content="", hash="h1")
    b = InstructionFile(path="B", content="", hash="h2")
    # 同序列必然同 hash
    assert aggregate_hash((a, b)) == aggregate_hash((a, b))
    # 内容不同则 hash 不同（有序敏感）
    assert aggregate_hash((a, b)) != aggregate_hash((b, a))
