"""记事本服务：CRUD / 目录树 / 修订 / 级联删除。"""
from __future__ import annotations

import pytest

from app.services.notes_service import NotesService


@pytest.fixture
def notes(tmp_path):
    return NotesService(tmp_path)


def test_create_get_update_delete(notes):
    n = notes.create(title="未命名文档", body_md="# hi\n")
    assert n["id"].startswith("note_")
    assert n["title"] == "未命名文档"
    got = notes.get(n["id"])
    assert "# hi" in got["body_md"]
    upd = notes.update(n["id"], title="周报", body_md="## x\n")
    assert upd["title"] == "周报"
    notes.delete(n["id"])
    with pytest.raises(FileNotFoundError):
        notes.get(n["id"])


def test_folder_tree_and_move_note(notes):
    f1 = notes.create_folder("工作")
    f2 = notes.create_folder("项目A", parent_id=f1["id"])
    n = notes.create(title="需求", folder_id=f2["id"])
    tree = notes.get_tree()
    assert any(x["id"] == f2["id"] for x in tree["folders"])
    assert any(x["id"] == n["id"] and x["folder_id"] == f2["id"] for x in tree["notes"])
    notes.update(n["id"], folder_id=f1["id"])
    assert notes.get(n["id"])["folder_id"] == f1["id"]


def test_create_folder_stale_parent_falls_back_to_root(notes):
    """已删/无效 parent_id 不应报「文件夹不存在」，落到根目录。"""
    f = notes.create_folder("面试", parent_id="fold_deadbeef")
    assert f["name"] == "面试"
    assert f["parent_id"] is None
    n = notes.create(title="草稿", folder_id="fold_deadbeef")
    assert n["folder_id"] is None


def test_search_title_and_body(notes):
    notes.create(title="周报模板", body_md="本周无进展\n")
    notes.create(title="闲聊", body_md="提到了周报与复盘\n")
    notes.create(title="无关", body_md="hello\n")
    empty = notes.search("  ")
    assert empty["total"] == 0
    r = notes.search("周报")
    assert r["total"] == 2
    ids_titles = {h["title"] for h in r["hits"]}
    assert "周报模板" in ids_titles
    assert "闲聊" in ids_titles
    # 标题命中排在正文命中前
    assert r["hits"][0]["title"] == "周报模板"
    assert "title" in r["hits"][0]["match_in"]
    body_hit = next(h for h in r["hits"] if h["title"] == "闲聊")
    assert "body" in body_hit["match_in"]
    assert "周报" in body_hit["snippet"]


def test_delete_folder_cascades(notes):
    f1 = notes.create_folder("父")
    f2 = notes.create_folder("子", parent_id=f1["id"])
    n = notes.create(title="将被删", folder_id=f2["id"])
    result = notes.delete_folder(f1["id"])
    assert f1["id"] in result["deleted_folders"]
    assert f2["id"] in result["deleted_folders"]
    assert n["id"] in result["deleted_notes"]
    with pytest.raises(FileNotFoundError):
        notes.get(n["id"])


def test_cannot_move_folder_into_descendant(notes):
    a = notes.create_folder("A")
    b = notes.create_folder("B", parent_id=a["id"])
    with pytest.raises(ValueError):
        notes.update_folder(a["id"], parent_id=b["id"])


def test_revision_and_restore(notes):
    n = notes.create(body_md="v1\n")
    notes.apply_edit(n["id"], op="replace_document", content_md="v2\n")
    assert notes.get(n["id"])["body_md"].startswith("v2")
    revs = notes.list_revisions(n["id"])
    assert revs
    assert "preview" in revs[0]
    detail = notes.get_revision(n["id"], revs[0]["id"])
    assert detail["body_md"].startswith("v1")
    restored = notes.restore_revision(n["id"], revs[0]["id"])
    assert restored["body_md"].startswith("v1")


def test_list_revisions_limit_and_body(notes):
    n = notes.create(body_md="base\n")
    for i in range(4):
        notes.create_revision(n["id"], source="ai")
        notes.update(n["id"], body_md=f"v{i}\n")
    recent = notes.list_revisions(n["id"], limit=10, include_body=True)
    assert len(recent) == 4
    assert all("body_md" in r for r in recent)
    assert all("preview" in r for r in recent)


def test_create_revision_skips_empty_and_duplicate(notes):
    n = notes.create(title="空", body_md="")
    assert notes.create_revision(n["id"], source="ai") is None
    notes.update(n["id"], body_md="有内容\n")
    first = notes.create_revision(n["id"], source="checkpoint")
    assert first and first["id"].startswith("rev_")
    # 正文未变 → 复用，不新增
    again = notes.create_revision(n["id"], source="checkpoint")
    assert again["id"] == first["id"]
    listed = notes.list_revisions(n["id"], include_body=True)
    assert len(listed) == 1
    assert listed[0]["char_count"] > 0


def test_replace_selection(notes):
    n = notes.create(body_md="hello world\n")
    out = notes.apply_edit(
        n["id"], op="replace_selection", content_md="there", selected_text="world")
    assert "hello there" in out["body_md"]


def test_insert_at_cursor_uses_anchor(notes):
    n = notes.create(body_md="hello world\n")
    notes.set_cursor(n["id"], before="hello ", after="world")
    out = notes.apply_edit(
        n["id"], op="insert_at_cursor", content_md="brave ")
    assert out["body_md"].startswith("hello brave world")
    assert out.get("inserted_at") == "cursor"


def test_insert_at_cursor_insert_after_override(notes):
    n = notes.create(body_md="aaa BBB ccc\n")
    notes.set_cursor(n["id"], before="aaa ", after="BBB")
    out = notes.apply_edit(
        n["id"],
        op="insert_at_cursor",
        content_md="XXX ",
        insert_after="BBB ",
    )
    assert "BBB XXX ccc" in out["body_md"]
    assert out.get("inserted_at") == "cursor"


def test_insert_at_cursor_falls_back_to_append(notes):
    n = notes.create(body_md="only\n")
    # 无锚点 → 文末追加
    out = notes.apply_edit(n["id"], op="insert_at_cursor", content_md="tail")
    assert out["body_md"].rstrip("\n").endswith("tail")
    assert out.get("inserted_at") == "append_fallback"


def test_asset_save(notes):
    n = notes.create()
    info = notes.save_asset(n["id"], b"\x89PNG\r\n", "x.png")
    assert info["filename"].startswith("img_")
    path = notes.get_asset_path(n["id"], info["filename"])
    assert path.is_file()
