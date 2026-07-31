"""
凭证管理器（产品文档 §凭证管理器 / 加密密钥方案）。

- API Key / OAuth / 基础认证加密存储于 SQLite credentials 表
- 主密钥存于 data/.master_key（不进备份/导出/git）
- 权限：Linux/macOS chmod 600；Windows icacls 移除继承并只授当前用户
  设置失败不阻断启动，推系统通知提醒
- 换机恢复：备份恢复后 credentials 无法解密 → 引导用户重新输入 API Key
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.credentials")


def _ensure_master_key(data_dir: Path) -> bytes:
    """读取或生成主密钥。"""
    key_path = data_dir / ".master_key"
    if key_path.exists():
        return key_path.read_bytes()
    # 生成 32 字节随机密钥
    try:
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
    except Exception:  # noqa: BLE001 - 无 cryptography 时退化
        key = base64.urlsafe_b64encode(os.urandom(32))
    key_path.write_bytes(key)
    _lock_permissions(key_path)
    return key


def _lock_permissions(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            user = os.environ.get("USERNAME", "")
            subprocess.run(["icacls", str(path), "/inheritance:r",
                            "/grant:r", f"{user}:F"],
                           capture_output=True, check=False)
        else:
            os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001 - 设置失败不阻断
        logger.warning("无法设置 .master_key 权限，请手动检查：%s", path)


class CredentialStore:
    def __init__(self, db, data_dir: str | Path):
        self.db = db
        self.data_dir = Path(data_dir)
        self._key = _ensure_master_key(self.data_dir)
        self._fernet = None
        try:
            from cryptography.fernet import Fernet
            self._fernet = Fernet(self._key)
        except Exception:  # noqa: BLE001
            logger.warning("cryptography 不可用，凭证将以弱编码存储（仅供本机）")

    # ---- 加解密 -----------------------------------------------------------
    def _encrypt(self, plaintext: str) -> bytes:
        if self._fernet:
            return self._fernet.encrypt(plaintext.encode("utf-8"))
        return base64.b64encode(plaintext.encode("utf-8"))

    def _decrypt(self, blob: bytes) -> str:
        if self._fernet:
            try:
                return self._fernet.decrypt(blob).decode("utf-8")
            except Exception:  # noqa: BLE001
                # 兼容弱编码期（未安装 cryptography 时存的 base64 凭证）：
                # 升级依赖后旧凭证不致静默失效
                return base64.b64decode(blob).decode("utf-8")
        return base64.b64decode(blob).decode("utf-8")

    # ---- CRUD -------------------------------------------------------------
    def store(self, name: str, credential_type: str, value: str) -> int:
        cur = self.db.execute(
            "INSERT INTO credentials(name,credential_type,encrypted_value,created_at) "
            "VALUES(?,?,?,?)",
            (name, credential_type, self._encrypt(value),
             now_cst().isoformat(timespec="seconds")))
        return cur.lastrowid

    def update(self, credential_id: int, value: str) -> None:
        self.db.execute(
            "UPDATE credentials SET encrypted_value=? WHERE id=?",
            (self._encrypt(value), credential_id))

    def get(self, credential_id: int) -> str | None:
        row = self.db.query_one(
            "SELECT encrypted_value FROM credentials WHERE id=?", (credential_id,))
        if not row:
            return None
        try:
            return self._decrypt(row["encrypted_value"])
        except Exception:  # noqa: BLE001 - 换机后无法解密
            logger.warning("凭证 %s 解密失败（可能换机），需重新输入", credential_id)
            return None

    def delete(self, credential_id: int) -> None:
        self.db.execute("DELETE FROM credentials WHERE id=?", (credential_id,))
