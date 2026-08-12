"""迁移 017 + 新参数 + FolderScanner 逻辑冒烟验证（不依赖 LLM）。"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

from scheduler.folder_scan import FolderScanner  # noqa: E402

from infrastructure.db import Database  # noqa: E402
from infrastructure.config_manager import PARAM_SCHEMA  # noqa: E402

tmp = pathlib.Path(tempfile.mkdtemp())
db = Database(tmp / "t.db")
db.run_migrations(str(pathlib.Path("migrations")))

# 1) 表结构
rows = db.query_all(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name IN ('local_dirs','local_dir_files')")
names = {r["name"] for r in rows}
assert {"local_dirs", "local_dir_files"} <= names, names
print("[1] 迁移 017 表结构 OK")

db.execute(
    "INSERT INTO local_dirs(path,enabled,recursive,created_at) "
    "VALUES('C:/test',1,1,'2026-08-02 00:00:00')")
db.execute(
    "INSERT INTO local_dir_files(dir_id,path,fingerprint,status,last_seen_at) "
    "VALUES(1,'C:/test/a.md','1_2','imported','2026-08-02 00:00:00')")
assert db.query_one("SELECT count(*) c FROM local_dir_files")["c"] == 1
print("[2] 表写入/索引 OK")

# 2) 新参数
keys = {p["key"] for p in PARAM_SCHEMA}
assert {"local_dir_scan_interval_hours", "local_dir_max_files_per_scan",
        "local_dir_include_images"} <= keys
print("[3] PARAM_SCHEMA 新参数 OK")

# 3) FolderScanner 冒烟：目录管理 + 指纹增量扫描（mock ingest 不调 LLM）

src = pathlib.Path(tempfile.mkdtemp())
(src / "笔记").mkdir()
(src / "笔记" / "a.md").write_text("用户喜欢本地优先的工具，偏好离线可用。", encoding="utf-8")
(src / "note.txt").write_text("这是一个关于部署架构的笔记：单体优先。", encoding="utf-8")
(src / ".hidden.md").write_text("应被跳过", encoding="utf-8")
(src / "node_modules").mkdir()
(src / "node_modules" / "x.md").write_text("应被跳过", encoding="utf-8")
(src / "big.bin").write_bytes(b"\x00" * 1024)  # 不支持的扩展名应被跳过
# data 目录排除校验
data_dir = tmp / "data"
data_dir.mkdir()
(data_dir / "memories").mkdir()
(data_dir / "memories" / "m.md").write_text("x", encoding="utf-8")


class FakeIngest:
    """不调用 LLM 的假 ingest：直接返回模拟结果并记录调用。"""

    def __init__(self, data_dir):
        self.calls = []
        self.data_dir = data_dir

    async def ingest_file(self, filename, content, source):
        assert source == "local_dir", source
        self.calls.append((filename, content.decode("utf-8", "ignore")))
        import hashlib
        return {"doc_id": "doc_" + hashlib.md5(filename.encode()).hexdigest()[:4],
                "extracted": 2}


class FakeConfig:
    def get(self, key, default=None):
        return {"local_dir_max_files_per_scan": 50,
                "local_dir_include_images": False}.get(key, default)


scanner = FolderScanner(db, data_dir, FakeIngest(data_dir), FakeConfig())

# 禁止接入 data 目录
try:
    scanner.add_dir(str(data_dir))
    raise AssertionError("应拒绝接入 data 目录")
except ValueError as e:
    print("[4] data 目录拒绝 OK:", e)

# 添加目录
item = scanner.add_dir(str(src), recursive=True)
assert item["id"] == 2
try:
    scanner.add_dir(str(src))
    raise AssertionError("应拒绝重复接入")
except ValueError:
    print("[5] 目录添加/去重 OK")

# 扫描（只取 src 目录的结果；C:/test 假目录应 skipped 且不影响）
# 注意：add_dir 内部 resolve() 为长路径，Windows 短路径（ADMINI~1）须同样 resolve 后再比对
src_key = str(src.resolve())
result = asyncio.run(scanner.scan_all(trigger="manual"))
d = next(x for x in result["dirs"] if x["path"] == src_key)
assert not d.get("skipped"), d
# 笔记/a.md + note.txt，.hidden/node_modules 被过滤
assert d["summary"]["imported"] == 2, d
assert d["summary"]["candidates"] == 2, d
contents = [c[1] for c in scanner.ingest.calls]
assert any(c.startswith("用户喜欢本地优先") for c in contents)
assert any(c.startswith("这是一个关于部署架构的笔记") for c in contents)
print("[6] 首轮扫描 OK:", json.dumps(d["summary"], ensure_ascii=False))

# 幂等：再扫一次无新文件
result = asyncio.run(scanner.scan_all(trigger="manual"))
d2 = next(x for x in result["dirs"] if x["path"] == src_key)
assert d2["summary"]["imported"] == 0 and d2["summary"]["changed"] == 0, d2
print("[7] 指纹幂等（无变更不重复导入）OK")

# 变更检测：修改 note.txt 后重扫 → 重新导入；删除 a.md → 标记 deleted
(src / "note.txt").write_text("更新后的内容：加入缓存层设计。", encoding="utf-8")
(src / "笔记" / "a.md").unlink()
result = asyncio.run(scanner.scan_all(trigger="manual"))
d3 = next(x for x in result["dirs"] if x["path"] == src_key)
assert d3["summary"]["changed"] == 1, d3   # 仅 note.txt 变更；已删除文件不计入 changed
assert d3["summary"]["imported"] == 1, d3   # 变更文件重新导入
assert d3["summary"]["deleted"] == 1, d3    # 删除文件标记
rows = db.query_all("SELECT path,status FROM local_dir_files "
                    "WHERE dir_id=2 ORDER BY id")
status_map = {r["path"].replace(src_key, ""): r["status"] for r in rows}
assert status_map == {"\\笔记\\a.md": "deleted",
                      "\\note.txt": "imported"}, status_map
print("[8] 变更重导 + 删除标记 OK:", status_map)

# 文件恢复：a.md 重新出现且内容未变 → 状态回到 imported（复用原关联）
(src / "笔记" / "a.md").write_text("用户喜欢本地优先的工具，偏好离线可用。", encoding="utf-8")
result = asyncio.run(scanner.scan_all(trigger="manual"))
row = db.query_one(
    "SELECT status FROM local_dir_files WHERE path=?",
    (str(pathlib.Path(src_key) / "笔记" / "a.md"),))
assert row and row["status"] == "imported", row
print("[9] 文件恢复状态回滚 OK")

# 文件列表 API 查询
files = db.query_all(
    "SELECT path,status FROM local_dir_files WHERE dir_id=? "
    "ORDER BY last_seen_at DESC LIMIT 500", (2,))
assert len(files) == 2
print("[10] 文件列表查询 OK, total:", len(files))

db.close()
print("\n=== 全部冒烟验证通过 ===")
