"""Real DB acceptance for schema v2 and the manual reset; loopback fixtures only."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from deploy.personal.migrate import require_backend_schema
from deploy.transitions.reset_provider_backend import CONFIRMATION, SQL_FILE, transition
from tests import test_database_migrations_postgres as migration_fixtures
from tests.test_hosted_backend import settings
from tests.test_provider_connections import api_body
from tradingagents.service.backend.contracts import StartAnalysis
from tradingagents.service.backend.repository import Repository
from tradingagents.service.backend.security import SecretVault
from tradingagents.service.errors import AnalysisServiceError

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    yield from migration_fixtures.cluster.__wrapped__(tmp_path_factory)


@pytest.fixture
def baseline_database(cluster):
    yield from migration_fixtures.database.__wrapped__(cluster)


@pytest.fixture
def db(baseline_database):
    conn, dsn = baseline_database
    conn.execute(SQL_FILE.read_text(), prepare=False)
    a, b = uuid4(), uuid4()
    conn.execute("INSERT INTO auth.users VALUES (%s),(%s)", (a, b))
    conn.execute(
        "ALTER TABLE public.instruments ADD COLUMN symbol text, ADD COLUMN is_active boolean DEFAULT true"
    )
    conn.execute(
        "INSERT INTO public.instruments(id,market,asset_type,symbol) VALUES (%s,'NASDAQ100','stock','NVDA')",
        (uuid4(),),
    )
    s = settings(
        database_url=dsn,
        allowed_user_ids=(a, b),
        allowed_models={"openai": ("fast-model", "deep-model"), "anthropic": ("deep-model",)},
    )
    repo = Repository(s)
    vault = SecretVault(s)
    keys = {}
    for user, provider, name in [
        (a, "openai", "A"),
        (a, "anthropic", "B"),
        (b, "openai", "C"),
        (a, "openai", "D"),
    ]:
        key = uuid4()
        repo.add_connection(
            user,
            key,
            provider,
            vault.encrypt("PRIVATE_" + name, user_id=user, connection_id=key, provider=provider),
            name=name,
        )
        keys[name] = key
    return SimpleNamespace(conn=conn, dsn=dsn, a=a, b=b, repo=repo, s=s, keys=keys)


def reserve(db, a="A", b="B", user=None, key=None):
    return db.repo.reserve(
        user or db.a, key or uuid4(), StartAnalysis(**api_body(db.keys[a], db.keys[b]))
    )


def test_cross_provider_reservation_is_atomic_and_snapshot_is_fixed(db):
    row, created = reserve(db)
    assert created and "connection_id" not in row
    assert row["model_settings"] == {
        "quick": {"provider": "openai", "model": "fast-model"},
        "deep": {"provider": "anthropic", "model": "deep-model"},
    }
    bindings = db.repo.execution_bindings(db.a, row["id"])
    assert {b["tier"]: b["id"] for b in bindings} == {"quick": db.keys["A"], "deep": db.keys["B"]}
    assert db.repo.execution_bindings(db.b, row["id"]) == []
    assert all("secret_ciphertext" not in item for item in db.repo.connections(db.a))


def test_same_provider_can_use_same_or_distinct_connections(db):
    row, _ = reserve(db, b="D")
    assert len(db.repo.execution_bindings(db.a, row["id"])) == 2
    db.repo.dispatch_failed(db.a, row["id"])
    row, _ = reserve(db, b="A")
    assert {b["id"] for b in db.repo.execution_bindings(db.a, row["id"])} == {db.keys["A"]}


def test_foreign_or_revoked_connection_cannot_create_any_job(db):
    for key in (db.keys["C"], uuid4()):
        with pytest.raises(AnalysisServiceError, match="CONNECTION_NOT_FOUND"):
            db.repo.reserve(db.a, uuid4(), StartAnalysis(**api_body(db.keys["A"], key)))
    assert db.conn.execute("SELECT count(*) FROM analysis_backend.jobs").fetchone()[0] == 0
    db.repo.revoke_connection(db.a, db.keys["B"])
    with pytest.raises(AnalysisServiceError, match="CONNECTION_NOT_FOUND"):
        reserve(db)


def test_revoke_keeps_history_and_clears_secret_without_switching_key(db):
    row, _ = reserve(db)
    db.repo.revoke_connection(db.b, db.keys["A"])
    assert len(db.repo.execution_bindings(db.a, row["id"])) == 2
    db.repo.revoke_connection(db.a, db.keys["A"])
    assert len(db.repo.execution_bindings(db.a, row["id"])) == 1
    assert db.repo.job(db.a, row["id"])["model_settings"]["quick"]["provider"] == "openai"
    assert db.conn.execute(
        "SELECT status,secret_ciphertext FROM analysis_backend.connections WHERE id=%s",
        (db.keys["A"],),
    ).fetchone() == ("revoked", None)


def test_idempotency_includes_both_connection_selections(db):
    key = uuid4()
    row, _ = reserve(db, key=key)
    replay, created = reserve(db, key=key)
    assert replay["id"] == row["id"] and not created
    with pytest.raises(AnalysisServiceError, match="IDEMPOTENCY_CONFLICT"):
        reserve(db, b="D", key=key)


def test_database_rejects_foreign_binding_incomplete_bindings_and_provider_drift(db):
    row, _ = reserve(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.conn.execute(
            "UPDATE analysis_backend.job_model_bindings SET connection_id=%s WHERE job_id=%s AND tier='quick'",
            (db.keys["C"], row["id"]),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.conn.transaction():
        new_id = uuid4()
        db.conn.execute(
            """INSERT INTO analysis_backend.jobs
            (id,user_id,request_key,request_hash,request,model_settings,status,lease_until,hard_deadline)
            SELECT %s,user_id,%s,request_hash,request,model_settings,'reserved',lease_until,hard_deadline
            FROM analysis_backend.jobs WHERE id=%s""",
            (new_id, uuid4(), row["id"]),
        )
        db.conn.execute(
            "INSERT INTO analysis_backend.job_model_bindings VALUES (%s,%s,'quick',%s)",
            (db.a, new_id, db.keys["C"]),
        )
        db.conn.execute(
            "SET CONSTRAINTS analysis_backend.job_model_bindings_user_id_connection_id_fkey IMMEDIATE"
        )
    with pytest.raises(psycopg.errors.CheckViolation), db.conn.transaction():
        db.conn.execute(
            "DELETE FROM analysis_backend.job_model_bindings WHERE job_id=%s AND tier='quick'",
            (row["id"],),
        )
        db.conn.execute(
            "INSERT INTO analysis_backend.job_model_bindings VALUES (%s,%s,'quick',%s)",
            (db.a, row["id"], db.keys["D"]),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        db.conn.execute(
            "DELETE FROM analysis_backend.job_model_bindings WHERE job_id=%s AND tier='quick'",
            (row["id"],),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        db.conn.execute(
            "UPDATE analysis_backend.connections SET provider='anthropic' WHERE id=%s",
            (db.keys["A"],),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        db.conn.execute(
            "UPDATE analysis_backend.jobs SET model_settings=jsonb_set(model_settings,'{quick,provider}','\"anthropic\"') WHERE id=%s",
            (row["id"],),
        )


def test_owner_only_personal_mode_has_no_api_bindings(db, tmp_path):
    from tests.test_personal_backend import personal_settings

    personal = personal_settings(
        tmp_path, personal_user_id=db.a, allowed_user_ids=(db.b,), database_url=db.dsn
    )
    repo = Repository(personal)
    repo.replace_provider_models("chatgpt", [("Session", "gpt-5.6-sol")])
    body = {
        "request": {"ticker": "NVDA", "analysis_date": "2026-01-01"},
        "auth_type": "personal_chatgpt",
        "quick_model": "gpt-5.6-sol",
        "deep_model": "gpt-5.6-sol",
    }
    with pytest.raises(AnalysisServiceError, match="ACCESS_DENIED"):
        repo.reserve(db.b, uuid4(), StartAnalysis(**body))
    row, _ = repo.reserve(db.a, uuid4(), StartAnalysis(**body))
    assert repo.execution_bindings(db.a, row["id"]) == []


def test_discovery_and_claim_include_invited_users_and_reject_outsiders(db):
    row, _ = reserve(db, a="C", b="C", user=db.b)
    assert db.repo.next_reserved() == row["id"]
    outsider = Repository(db.s.model_copy(update={"allowed_user_ids": ()}))
    assert outsider.next_reserved() is None
    with pytest.raises(AnalysisServiceError, match="WORKER_CLAIM_REJECTED"):
        outsider.claim(row["id"])
    assert db.repo.claim(row["id"])["user_id"] == db.b


def test_single_global_capacity_still_fences_parallel_admission(db):
    def attempt(_):
        try:
            return reserve(db)[1]
        except AnalysisServiceError as e:
            return e.code

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(attempt, range(5)))
    assert results.count(True) == 1 and results.count("CAPACITY_FULL") == 4


def test_private_tables_cannot_be_read_by_mobile_roles(db):
    require_backend_schema(db.conn, 2)
    for role in ("anon", "authenticated"):
        for name in ("connections", "jobs", "job_model_bindings", "memory", "schema_revision"):
            assert not db.conn.execute(
                "SELECT has_table_privilege(%s,%s,'SELECT,INSERT,UPDATE,DELETE')",
                (role, "analysis_backend." + name),
            ).fetchone()[0]


def test_reset_dry_run_and_guard_do_not_mutate_baseline(baseline_database):
    conn, dsn = baseline_database
    s = settings(database_url=dsn)

    class Storage:
        def discard(self, path):
            pytest.fail("Dry-run must never call Storage")

    with pytest.raises(ValueError, match="Manual backend"):
        require_backend_schema(conn, 2)
    plan = transition(conn, s, Storage())
    assert plan["status"] == "planned" and len(plan["plan_hash"]) == 64
    assert (
        conn.execute("SELECT to_regclass('analysis_backend.schema_revision')").fetchone()[0] is None
    )
    with pytest.raises(ValueError, match="confirmation"):
        transition(conn, s, Storage(), apply=True)
    with pytest.raises(ValueError, match="changed"):
        transition(
            conn,
            s,
            Storage(),
            apply=True,
            plan_hash="0" * 64,
            confirmation=CONFIRMATION,
            quiesced=True,
        )
    result = transition(
        conn,
        s,
        Storage(),
        apply=True,
        plan_hash=plan["plan_hash"],
        confirmation=CONFIRMATION,
        quiesced=True,
    )
    assert result["schema_revision"] == 2
    require_backend_schema(conn, 2)
    with pytest.raises(ValueError, match="already applied"):
        transition(conn, s, Storage())


def test_storage_failure_preserves_sql_data_and_stops_schema_reset(baseline_database):
    conn, dsn = baseline_database
    path = f"{uuid4()}/{uuid4()}/{uuid4()}.json"
    conn.execute(
        "INSERT INTO storage.objects(bucket_id,name) VALUES ('analysis-reports',%s)", (path,)
    )
    s = settings(database_url=dsn)
    storage = SimpleNamespace(discard=lambda _: False)
    plan = transition(conn, s, storage)
    with pytest.raises(ValueError, match="Report deletion failed"):
        transition(
            conn,
            s,
            storage,
            apply=True,
            confirmation=CONFIRMATION,
            quiesced=True,
            plan_hash=plan["plan_hash"],
        )
    assert (
        conn.execute("SELECT to_regclass('analysis_backend.schema_revision')").fetchone()[0] is None
    )
    assert (
        conn.execute("SELECT count(*) FROM supabase_migrations.schema_migrations").fetchone()[0]
        == 3
    )


def test_reset_refuses_unexpected_storage_path_before_any_deletion(baseline_database):
    conn, dsn = baseline_database
    conn.execute(
        "INSERT INTO storage.objects(bucket_id,name) VALUES ('analysis-reports','unrelated-file.txt')"
    )
    with pytest.raises(AnalysisServiceError, match="INVALID_REPORT_PATH"):
        transition(
            conn,
            settings(database_url=dsn),
            SimpleNamespace(discard=lambda _: pytest.fail("No delete")),
        )


def test_reset_removes_only_analysis_data_and_preserves_users_catalog_and_platform(
    baseline_database,
):
    conn, dsn = baseline_database
    user, connection, job = uuid4(), uuid4(), uuid4()
    conn.execute("INSERT INTO auth.users VALUES (%s)", (user,))
    conn.execute("INSERT INTO public.instruments VALUES (%s,'NASDAQ100','stock')", (uuid4(),))
    conn.execute(
        "INSERT INTO analysis_backend.provider_model_catalog(provider,models) VALUES ('chatgpt','[]')"
    )
    conn.execute(
        "INSERT INTO analysis_backend.connections(id,user_id,provider,secret_ciphertext,status,verified_at) VALUES (%s,%s,'openai','FAKE_CIPHER','active',now())",
        (connection, user),
    )
    conn.execute(
        """INSERT INTO analysis_backend.jobs(id,user_id,connection_id,request_key,request_hash,request,model_settings,status,finished_at,lease_until,hard_deadline)
        VALUES (%s,%s,%s,%s,'hash','{}','{}','failed',now(),now(),now()+interval '35 minutes')""",
        (job, user, connection, uuid4()),
    )
    conn.execute("INSERT INTO analysis_backend.rate_limits VALUES (%s,'api',now(),1)", (user,))
    path = f"{user}/{job}/{uuid4()}.json"
    conn.execute(
        "INSERT INTO storage.objects(bucket_id,name) VALUES ('analysis-reports',%s)", (path,)
    )
    deleted = []
    storage = SimpleNamespace(discard=lambda path: deleted.append(path) or True)
    s = settings(database_url=dsn)
    plan = transition(conn, s, storage)
    assert plan["counts"]["jobs"] == 1 and plan["counts"]["connections"] == 1
    assert not deleted
    transition(
        conn,
        s,
        storage,
        apply=True,
        confirmation=CONFIRMATION,
        quiesced=True,
        plan_hash=plan["plan_hash"],
    )
    assert deleted == [path]
    assert conn.execute("SELECT count(*) FROM auth.users").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM public.instruments").fetchone()[0] == 1
    assert (
        conn.execute("SELECT count(*) FROM analysis_backend.provider_model_catalog").fetchone()[0]
        == 1
    )
    for table in ("jobs", "connections", "memory", "rate_limits"):
        assert conn.execute(f"SELECT count(*) FROM analysis_backend.{table}").fetchone()[0] == 0


def test_reset_refuses_active_work_and_changed_connection_inventory(baseline_database):
    conn, dsn = baseline_database
    user, connection, job = uuid4(), uuid4(), uuid4()
    conn.execute("INSERT INTO auth.users VALUES (%s)", (user,))
    conn.execute(
        "INSERT INTO analysis_backend.connections(id,user_id,provider,secret_ciphertext,status,verified_at) VALUES (%s,%s,'openai','FAKE_CIPHER','active',now())",
        (connection, user),
    )
    s = settings(database_url=dsn)
    storage = SimpleNamespace(discard=lambda _: pytest.fail("No delete"))
    plan = transition(conn, s, storage)
    replacement = uuid4()
    conn.execute(
        "UPDATE analysis_backend.connections SET id=%s WHERE id=%s", (replacement, connection)
    )
    with pytest.raises(ValueError, match="changed"):
        transition(
            conn,
            s,
            storage,
            apply=True,
            confirmation=CONFIRMATION,
            quiesced=True,
            plan_hash=plan["plan_hash"],
        )
    conn.execute(
        """INSERT INTO analysis_backend.jobs(id,user_id,connection_id,request_key,request_hash,request,model_settings,status,lease_until,hard_deadline)
        VALUES (%s,%s,%s,%s,'hash','{}','{}','reserved',now()+interval '2 minutes',now()+interval '35 minutes')""",
        (job, user, replacement, uuid4()),
    )
    with pytest.raises(ValueError, match="Active analyses"):
        transition(conn, s, storage)


def test_user_deletion_cascades_without_deleting_another_users_connections(db):
    row, _ = reserve(db)
    db.conn.execute("DELETE FROM auth.users WHERE id=%s", (db.a,))
    assert (
        db.conn.execute(
            "SELECT count(*) FROM analysis_backend.jobs WHERE id=%s", (row["id"],)
        ).fetchone()[0]
        == 0
    )
    assert (
        db.conn.execute("SELECT count(*) FROM analysis_backend.job_model_bindings").fetchone()[0]
        == 0
    )
    assert db.repo.connections(db.b)[0]["id"] == db.keys["C"]


def test_real_db_child_resolves_mixed_provider_keys_without_leaking_to_config_or_output(
    db, tmp_path, monkeypatch
):
    from tests.test_hosted_backend import worker_result
    from tradingagents.llm_clients.tier_credentials import credential_for
    from tradingagents.service.backend import worker
    from tradingagents.service.memory import MemoryDecision

    row, _ = reserve(db)
    claimed = db.repo.claim(row["id"])

    class Engine:
        def __init__(self, *, settings, memory):
            self.settings, self.memory = settings, memory

        def run(self, request, *, user_id, analysis_id):
            assert credential_for("quick", "openai") == "PRIVATE_A"
            assert credential_for("deep", "anthropic") == "PRIVATE_B"
            assert "PRIVATE" not in self.settings.model_dump_json()
            result = worker_result(analysis_id)
            self.memory.save_decision(
                MemoryDecision(
                    analysis_id=analysis_id,
                    user_id=user_id,
                    ticker=request.ticker,
                    analysis_date=request.analysis_date,
                    rating="Overweight",
                    decision="Test",
                    created_at=result.started_at,
                    evaluation_trading_days=5,
                    benchmark="SPY",
                )
            )
            return result

    monkeypatch.setattr(worker, "AnalysisEngine", Engine)
    output = tmp_path / "result.json"
    worker.run_child(db.repo, row["id"], claimed["lease_token"], output)
    raw = output.read_text()
    assert "PRIVATE_A" not in raw and "PRIVATE_B" not in raw
    assert credential_for("quick", "openai") is None


def test_revocation_between_reservation_and_child_never_constructs_model(db, tmp_path, monkeypatch):
    from tradingagents.service.backend import worker

    row, _ = reserve(db)
    claimed = db.repo.claim(row["id"])
    db.repo.revoke_connection(db.a, db.keys["B"])
    monkeypatch.setattr(
        worker, "AnalysisEngine", lambda **kwargs: pytest.fail("No model may be constructed")
    )
    with pytest.raises(AnalysisServiceError, match="CONNECTION_NOT_FOUND"):
        worker.run_child(db.repo, row["id"], claimed["lease_token"], tmp_path / "failure.json")


def test_packaged_migration_check_requires_v2_before_any_ledger_write(
    baseline_database, monkeypatch, capsys
):
    from deploy.personal import migrate
    from tests.test_database_migrations import SOURCE
    from tradingagents.service.backend import runtime

    conn, dsn = baseline_database
    monkeypatch.setattr(runtime, "settings_from_env", lambda: settings(database_url=dsn))
    monkeypatch.setattr("sys.argv", ["runner", "--directory", str(SOURCE), "--check"])
    with pytest.raises(SystemExit) as e:
        migrate.main()
    assert e.value.code == 1
    assert "DATABASE_MIGRATION_FAILED" in capsys.readouterr().out
    assert (
        conn.execute("SELECT to_regclass('tradingagents_deploy.migrations')").fetchone()[0] is None
    )
    conn.execute(SQL_FILE.read_text(), prepare=False)
    migrate.main()
    assert "checked" in capsys.readouterr().out
