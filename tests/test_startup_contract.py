"""启动器与 Windows 凭据权限的回归契约。"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_start_command_supports_an_isolated_data_directory():
    source = (ROOT / "start.py").read_text(encoding="utf-8")
    assert 'parser.add_argument("--data-dir"' in source
    assert "DATA_DIR = args.data_dir.resolve()" in source


def test_windows_key_lock_uses_running_principal_not_only_environment_name():
    source = (ROOT / "connectors/credential_store.py").read_text(encoding="utf-8")
    assert 'subprocess.run(["whoami"]' in source
    assert "completed.stdout.strip()" in source
