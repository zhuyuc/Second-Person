"""安装后 / 手动：确保 System One Provider 存在并绑定 mood_fast。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--force-bind", action="store_true",
        help="即使 mood_fast 已有绑定，也改绑到 System One",
    )
    args = p.parse_args()

    from connectors.credential_store import CredentialStore
    from infrastructure.db import Database
    from infrastructure.provider_registry import (
        ProviderRegistry, ensure_mood_fast_system_one_assignment,
        _system_one_exe_ready,
    )

    if not _system_one_exe_ready():
        print("system_one/venv 未就绪：请先运行 setup_local.ps1", file=sys.stderr)
        return 1

    data = ROOT / "data"
    data.mkdir(parents=True, exist_ok=True)
    db_path = data / "palace.db"
    if not db_path.exists():
        print(f"数据库不存在：{db_path}（请先至少启动一次 python start.py）",
              file=sys.stderr)
        return 1
    db = Database(db_path)
    db.run_migrations(ROOT / "migrations")
    try:
        creds = CredentialStore(db, data)
        reg = ProviderRegistry(db, creds)
        filled = ensure_mood_fast_system_one_assignment(
            reg, force_bind=args.force_bind)
        pid = reg.assignment("mood_fast")
        snap = reg.snapshot(pid) if pid else None
        print(f"filled={filled}")
        if snap:
            print(
                f"mood_fast -> {pid} type={snap.provider_type} "
                f"model={snap.model_id} url={snap.base_url}")
        else:
            print("mood_fast unbound")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
