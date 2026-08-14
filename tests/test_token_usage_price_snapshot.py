"""token_usage 单价快照冻结契约测试。

保护契约：035 迁移为 token_usage 增加单价快照/金额列并按当前单价回填历史用量；
TokenRecorder 记录用量时冻结当时单价并折算金额，此后调价不影响已落库金额；
未配单价的用量快照留空（费用查询按当时单价兜底，未配不计入）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from infrastructure.db import Database
from infrastructure.llm_provider import TokenRecorder

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    database.run_migrations(ROOT / "migrations")
    try:
        yield database
    finally:
        database.close()


def _seed_provider(db: Database, pid: str, model_id: str,
                   input_price: float | None, output_price: float | None) -> None:
    db.execute(
        "INSERT INTO providers(id,display_name,provider_type,base_url,model_id,"
        "credential_id,input_price,output_price,context_window,status,created_at) "
        "VALUES(?,?,?,?,?,0,?,?,128000,'healthy','2026-01-01T00:00:00')",
        (pid, pid, "openai_compatible", "http://localhost", model_id,
         input_price, output_price))


def _seed_legacy_usage(db: Database, model_name: str, inp: int, outp: int) -> None:
    db.execute(
        "INSERT INTO token_usage(model_name,source,session_id,input_tokens,"
        "output_tokens,trace_id,create_time) VALUES(?,?,?,?,?,?,?)",
        (model_name, "main_chat", "sess_1", inp, outp,
         "t1", "2026-01-05T10:00:00"))


def test_migration_backfills_legacy_usage_with_current_price(tmp_path: Path):
    # 先只应用到 034，构造"迁移前"的历史数据，再应用 035 验证回填
    legacy_dir = tmp_path / "migrations_legacy"
    legacy_dir.mkdir()
    for f in sorted((ROOT / "migrations").glob("*.sql")):
        if f.stem < "035":
            shutil.copy(f, legacy_dir / f.name)

    database = Database(tmp_path / "backfill.db")
    try:
        database.run_migrations(legacy_dir)
        _seed_provider(database, "prov_001", "model-a", 2.0, 8.0)
        _seed_provider(database, "prov_002", "model-free", None, None)
        _seed_legacy_usage(database, "model-a", 1_000_000, 500_000)
        _seed_legacy_usage(database, "model-free", 1000, 1000)
        _seed_legacy_usage(database, "model-gone", 1000, 1000)  # Provider 已删除

        database.run_migrations(ROOT / "migrations")   # 仅补跑 035

        priced = database.query_one(
            "SELECT input_price, output_price, cost FROM token_usage "
            "WHERE model_name='model-a'")
        assert priced["input_price"] == 2.0
        assert priced["output_price"] == 8.0
        assert priced["cost"] == pytest.approx(1.0 * 2.0 + 0.5 * 8.0)

        # 未配单价 / Provider 已删除：快照留空，费用查询按当时单价兜底
        for model in ("model-free", "model-gone"):
            row = database.query_one(
                "SELECT input_price, output_price, cost FROM token_usage "
                "WHERE model_name=?", (model,))
            assert row["input_price"] is None
            assert row["cost"] is None
    finally:
        database.close()


def test_recorder_freezes_price_snapshot(db: Database):
    _seed_provider(db, "prov_001", "model-a", 2.0, 8.0)
    recorder = TokenRecorder(db)
    recorder.record("model-a", "main_chat", 1_000_000, 500_000, "sess_1",
                    input_price=2.0, output_price=8.0)
    recorder.record("model-free", "embedding", 1000, 1000, None)
    db.execute("UPDATE token_usage SET trace_id=trace_id WHERE 0")  # 屏障：等待火忘式写入落库

    priced = db.query_one(
        "SELECT input_price, output_price, cost FROM token_usage "
        "WHERE model_name='model-a'")
    assert priced["input_price"] == 2.0
    assert priced["cost"] == pytest.approx(6.0)

    free = db.query_one(
        "SELECT input_price, output_price, cost FROM token_usage "
        "WHERE model_name='model-free'")
    assert free["cost"] is None


def test_price_change_does_not_rewrite_history(db: Database):
    _seed_provider(db, "prov_001", "model-a", 2.0, 8.0)
    recorder = TokenRecorder(db)
    recorder.record("model-a", "main_chat", 1_000_000, 0, "sess_1",
                    input_price=2.0, output_price=8.0)
    # 调价：历史金额必须保持用量发生时的口径
    db.execute("UPDATE providers SET input_price=10.0 WHERE id='prov_001'")
    db.execute("UPDATE token_usage SET trace_id=trace_id WHERE 0")  # 屏障

    row = db.query_one(
        "SELECT cost FROM token_usage WHERE model_name='model-a'")
    assert row["cost"] == pytest.approx(2.0)
