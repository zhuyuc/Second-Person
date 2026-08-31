"""Second-Person 文件工具族（M3）。

模块划分：
- errors.py     : FsError + 错误码枚举
- resolver.py   : 路径归一化 + 围栏检查（含 TOCTOU 二次校验）
- ignore.py     : .gitignore + 默认忽略规则
- io.py         : 读/写/编辑底层（编码检测、二进制拒、原子写、字面 edit）
- diff.py       : unified diff 生成（写/编辑成功后返给前端渲染差异卡）
- observation.py: fs_observations 表读写（read-before-edit 版本乐观锁）
- policy.py     : 四档沙箱策略解析（含 session_policy_events fold）
- workspace.py  : WorkspaceResolver：session_id → WorkspaceContext
- tools.py      : 7 个 fs 工具（fs_read/fs_read_image/fs_list/fs_glob/fs_grep/
                                fs_write/fs_edit）+ register_fs_tools()
"""
from .workspace import WorkspaceContext, WorkspaceResolver  # noqa: F401
from .tools import register_fs_tools  # noqa: F401
