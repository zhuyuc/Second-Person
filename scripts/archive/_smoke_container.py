"""容器装配验证：临时数据目录构造 AppContainer，确认本地目录功能完整接线。"""
import os
import pathlib
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

tmp = pathlib.Path(tempfile.mkdtemp())

from app.container import AppContainer  # noqa: E402

c = AppContainer(tmp)

# 1) folder_scanner 已装配
assert hasattr(c, "folder_scanner")
print("[1] folder_scanner 装配 OK")

# 2) 调度任务已注册（register_task 落库 scheduled_tasks）
row = c.db.query_one(
    "SELECT task_id,name FROM scheduled_tasks WHERE task_id='local_dir_scan'")
assert row and row["task_id"] == "local_dir_scan", row
print("[2] 调度任务注册 OK:", row["name"])

# 3) 新参数可正常读取/更新
c.config.update_params({"local_dir_scan_interval_hours": 6,
                        "local_dir_max_files_per_scan": 20,
                        "local_dir_include_images": True})
assert c.config.get("local_dir_scan_interval_hours") == 6
assert c.config.get("local_dir_include_images") is True
print("[3] 新参数读写 OK")

# 4) 目录管理 API 层逻辑（add/list/set_enabled/remove 全链路）
src = pathlib.Path(tempfile.mkdtemp())
(src / "hello.md").write_text("测试内容", encoding="utf-8")
item = c.folder_scanner.add_dir(str(src), recursive=True)
assert item["id"] == 1
assert c.folder_scanner.list_dirs()[0]["path"] == item["path"]
c.folder_scanner.set_enabled(item["id"], False)
assert c.folder_scanner.list_dirs()[0]["enabled"] is False
c.folder_scanner.remove_dir(item["id"])
assert c.folder_scanner.list_dirs() == []
print("[4] 目录管理 CRUD OK")

# 5) 路由挂载验证（misc 路由注册无冲突）
from app.routes import misc  # noqa: E402
paths = {r.path for r in misc.router.routes}
assert "/import/local-dirs" in paths
assert "/import/local-dirs/scan" in paths
assert "/import/local-dirs/{dir_id}/files" in paths
print("[5] API 路由注册 OK")

c.db.close()
print("\n=== 容器装配验证通过 ===")
