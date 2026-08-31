"""FsError + 错误码枚举（v5 §六 6.2）。"""
from __future__ import annotations

from enum import Enum


class FsErrorCode(str, Enum):
    NOT_FOUND = "FS_NOT_FOUND"
    NOT_TEXT = "FS_NOT_TEXT"
    TOO_LARGE = "FS_TOO_LARGE"
    STALE_VERSION = "FS_STALE_VERSION"
    NOT_OBSERVED = "FS_NOT_OBSERVED"
    SANDBOX_DENIED = "FS_SANDBOX_DENIED"
    INVALID_PATH = "FS_INVALID_PATH"
    AMBIGUOUS_EDIT = "FS_AMBIGUOUS_EDIT"
    NOT_REGULAR_FILE = "FS_NOT_REGULAR_FILE"
    BINARY_UNSUPPORTED = "FS_BINARY_UNSUPPORTED"
    NO_MATCH = "FS_NO_MATCH"


# 模型侧建议：错误码 → 补救指令
FS_ERROR_HINTS = {
    FsErrorCode.STALE_VERSION: "文件已被外部修改，请先 fs_read 后再改",
    FsErrorCode.NOT_OBSERVED: "请先 fs_read 该文件再进行编辑",
    FsErrorCode.SANDBOX_DENIED: "当前沙箱策略不允许该操作，请让用户切换沙箱档位",
    FsErrorCode.AMBIGUOUS_EDIT: "old_string 匹配多处，请提供更长上下文或加 replace_all=true",
    FsErrorCode.NOT_TEXT: "该文件不是 UTF-8 文本，无法编辑；如为图片请用 fs_read_image",
    FsErrorCode.TOO_LARGE: "文件超过读写上限，请分块读或让用户手动处理",
    FsErrorCode.INVALID_PATH: "路径非法或越界",
}


class FsError(RuntimeError):
    """文件工具族统一错误。返给模型时用户可见 code + message。"""

    def __init__(self, code: FsErrorCode, message: str,
                 *, path: str | None = None):
        self.code = code
        self.path = path
        hint = FS_ERROR_HINTS.get(code)
        if hint and hint not in message:
            message = f"{message} — {hint}"
        super().__init__(message)

    def to_result(self) -> dict:
        return {"error": True, "code": self.code.value,
                "message": str(self), "path": self.path}
