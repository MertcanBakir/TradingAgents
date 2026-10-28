"""Real PostgreSQL acceptance. Ephemeral loopback cluster, never a remote DSN.

Run explicitly with -m integration. Applies the complete FinBot migration chain
against small Supabase platform stubs (auth users/uid, storage bucket metadata).
Auth/Storage HTTP behavior is tested separately with httpx's real client boundary.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.test_hosted_backend import settings
from tradingagents.service.backend.contracts import StartAnalysis
from tradingagents.service.backend.evaluation import EvaluationJob
from tradingagents.service.backend.repository import Repository, SupabaseMemoryRepository
from tradingagents.service.errors import AnalysisServiceError
from tradingagents.service.memory import MemoryDecision, MemoryOutcome

pytestmark = pytest.mark.integration


def test_personal_chatgpt_real_reservation_has_no_connection_or_secret(db, tmp_path):
    from tests.test_personal_backend import personal_settings

    repo = Repository(personal_settings(tmp_path, database_url=db.dsn, personal_user_id=db.a))
    repo.replace_provider_models("chatgpt", [("GPT 5.6 Sol", "gpt-5.6-sol")])
    payload = StartAnalysis(
        request={"ticker": "NVDA", "analysis_date": "2026-01-01"},
        auth_type="personal_chatgpt",
        quick_model="gpt-5.6-sol",
        deep_model="gpt-5.6-sol",
    )
    key = uuid4()
    row, created = repo.reserve(db.a, key, payload)
    assert created and row["auth_type"] == "personal_chatgpt" and row["connection_id"] is None
    assert repo.next_reserved() == row["id"]
    assert repo.reserve(db.a, key, payload)[1] is False
    with pytest.raises(AnalysisServiceError, match="ACCESS_DENIED"):
        repo.reserve(db.b, uuid4(), payload)
    # Separate fresh repository simulates API exit/restart before dispatch.
    fresh = Repository(repo.settings)
    assert fresh.next_reserved() == row["id"]
    claimed = fresh.claim(row["id"])
    assert claimed["status"] == "running"
    assert repo.next_reserved() is None
    with pytest.raises(AnalysisServiceError, match="WORKER_CLAIM_REJECTED"):
        repo.claim(row["id"])


def test_personal_real_parallel_admission_never_exceeds_one(db, tmp_path):
    from tests.test_personal_backend import personal_settings

    repo = Repository(personal_settings(tmp_path, database_url=db.dsn, personal_user_id=db.a))
    repo.replace_provider_models("chatgpt", [("GPT 5.6 Sol", "gpt-5.6-sol")])
    payload = StartAnalysis(
        request={"ticker": "NVDA", "analysis_date": "2026-01-01"},
        auth_type="personal_chatgpt",
        quick_model="gpt-5.6-sol",
        deep_model="gpt-5.6-sol",
    )
    barrier = Barrier(6)

    def attempt(_):
        barrier.wait()
        try:
            return repo.reserve(db.a, uuid4(), payload)[1]
        except AnalysisServiceError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    assert results.count(True) == 1
    assert results.count("CAPACITY_FULL") == 5


def test_personal_uid_change_never_migrates_old_records_or_claims_them(db, tmp_path):
    from tests.test_personal_backend import personal_settings

    row = db.reserve()
    repo = Repository(personal_settings(tmp_path, database_url=db.dsn, personal_user_id=db.b))
    assert repo.history(db.b, 20, 0) == ([], 0)
    assert repo.next_reserved() is None
    with pytest.raises(AnalysisServiceError, match="WORKER_CLAIM_REJECTED"):
        repo.claim(row["id"])
    assert db.repo.job(db.a, row["id"])["status"] == "reserved"


def test_personal_database_contract_rejects_missing_or_mixed_credentials(db):
    row = db.reserve()
    with pytest.raises(psycopg.errors.CheckViolation):
        db.sql("UPDATE analysis_backend.jobs SET connection_id=NULL WHERE id=%s", (row["id"],))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.sql(
            "UPDATE analysis_backend.jobs SET auth_type='personal_chatgpt',connection_id=NULL WHERE id=%s",
            (row["id"],),
        )
    assert db.repo.job(db.a, row["id"])["auth_type"] == "api_key"


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    bin_dir = Path(os.environ.get("TEST_PG_BIN", "/opt/homebrew/opt/postgresql@17/bin"))
    if not (bin_dir / "initdb").exists():
        found = shutil.which("initdb")
        if not found:
            pytest.skip("PostgreSQL binaries missing; set TEST_PG_BIN")
        bin_dir = Path(found).parent
    migrations = (
        Path(os.environ.get("FINBOT_ROOT", str(Path(__file__).resolve().parents[2] / "FinBot")))
        / "supabase/migrations"
    )
    if not migrations.is_dir():
        pytest.skip("FinBot migration chain missing; set FINBOT_ROOT")
    root = tmp_path_factory.mktemp("analysis-postgres")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    subprocess.run(
        [
            str(bin_dir / "initdb"),
            "-D",
            str(root / "db"),
            "-U",
            "postgres",
            "-A",
            "trust",
            "--no-locale",
            "-E",
            "UTF8",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            str(bin_dir / "pg_ctl"),
            "-D",
            str(root / "db"),
            "-l",
            str(root / "postgres.log"),
            "-o",
            f"-h 127.0.0.1 -p {port} -c unix_socket_directories=''",
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("""CREATE ROLE anon NOLOGIN; CREATE ROLE authenticated NOLOGIN;
                CREATE ROLE service_role NOLOGIN; CREATE SCHEMA auth; CREATE SCHEMA storage;
                CREATE TABLE auth.users(id uuid PRIMARY KEY);
                CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
                $$ SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
                GRANT USAGE ON SCHEMA auth TO anon,authenticated;
                CREATE TABLE storage.buckets(id text PRIMARY KEY,name text,public boolean,
                                            file_size_limit bigint,allowed_mime_types text[]);
                CREATE TABLE storage.objects(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                    bucket_id text,name text,created_at timestamptz DEFAULT now());
                ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;""")
            files = sorted(migrations.glob("*.sql"))
            assert len({p.name.split("_")[0] for p in files}) == len(files)
            for migration in files:
                conn.execute(migration.read_text(), prepare=False)
            # Run the repository's actual SQL security acceptance file too.
            conn.execute(
                (migrations.parents[1] / "tests/sql/analysis_backend_test.sql")
                .read_text()
                .replace("\\set ON_ERROR_STOP on", ""),
                prepare=False,
            )
        yield dsn
    finally:
        subprocess.run(
            [str(bin_dir / "pg_ctl"), "-D", str(root / "db"), "-m", "fast", "-w", "stop"],
            check=True,
            capture_output=True,
        )


@dataclass
class Fixture:
    dsn: str
    repo: Repository
    a: object
    b: object
    connection: object

    def sql(self, query, params=None):
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else []

    def request(self, **changes):
        return StartAnalysis(
            **(
                {
                    "request": {"ticker": "NVDA", "analysis_date": "2026-01-01"},
                    "connection_id": self.connection,
                    "quick_model": "gpt-5.6-sol",
                    "deep_model": "gpt-5.6-sol",
                }
                | changes
            )
        )

    def reserve(self, **changes):
        return self.repo.reserve(self.a, uuid4(), self.request(**changes))[0]

    def memory(self):
        job = self.reserve()
        claim = self.repo.claim(job["id"])
        d = MemoryDecision(
            analysis_id=job["id"],
            user_id=self.a,
            ticker="NVDA",
            analysis_date=date(2026, 1, 1),
            rating="Overweight",
            decision="A-private-decision",
            created_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
            evaluation_trading_days=5,
            benchmark="SPY",
        )
        self.repo.finish(
            self.a,
            job["id"],
            claim["lease_token"],
            status="success",
            report_path="fixture",
            digest="fixture",
            decision=d,
        )
        return d


@pytest.fixture
def db(cluster):
    a, b, c = uuid4(), uuid4(), uuid4()
    repo = Repository(settings(database_url=cluster))
    f = Fixture(cluster, repo, a, b, c)
    f.sql("INSERT INTO auth.users(id) VALUES(%s),(%s)", (a, b))
    f.sql(
        "INSERT INTO public.instruments(market,symbol,company_name,is_active,asset_type,currency) VALUES('NASDAQ100','NVDA','Nvidia',true,'stock','USD') ON CONFLICT(market,symbol) DO NOTHING"
    )
    repo.add_connection(a, c, "openai", "v1.ENCRYPTED_FIXTURE")
    try:
        yield f
    finally:
        f.sql("DELETE FROM auth.users WHERE id IN (%s,%s)", (a, b))


def test_complete_chain_applies_and_private_grants_hold(db):
    assert (
        db.sql("SELECT public FROM storage.buckets WHERE id='analysis-reports'")[0]["public"]
        is False
    )
    for role in ("anon", "authenticated"):
        for table in ("connections", "jobs", "memory", "rate_limits"):
            assert not db.sql(
                "SELECT has_table_privilege(%s,%s,'SELECT,INSERT,UPDATE,DELETE') AS allowed",
                (role, "analysis_backend." + table),
            )[0]["allowed"]
        with psycopg.connect(db.dsn, autocommit=True) as conn:
            conn.execute("SET ROLE " + role)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT * FROM analysis_backend.connections")


def test_reservation_is_committed_and_owner_isolation(db):
    row = db.reserve()
    assert db.repo.job(db.a, row["id"])["status"] == "reserved"
    assert db.repo.history(db.b, 20, 0) == ([], 0)
    assert db.repo.connections(db.b) == []
    with pytest.raises(AnalysisServiceError, match="ANALYSIS_NOT_FOUND"):
        db.repo.job(db.b, row["id"])
    with pytest.raises(AnalysisServiceError, match="CONNECTION_NOT_FOUND"):
        db.repo.connection(db.b, db.connection)
    db.repo.revoke_connection(db.b, db.connection)
    assert db.repo.connection(db.a, db.connection)["status"] == "active"


def test_real_parallel_capacity_never_exceeds_three(db):
    barrier = Barrier(6)

    def attempt(_):
        barrier.wait()
        try:
            return db.repo.reserve(db.a, uuid4(), db.request())[1]
        except AnalysisServiceError as e:
            return e.code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    assert results.count(True) == 3
    assert results.count("CAPACITY_FULL") == 3
    assert (
        db.sql(
            "SELECT count(*) AS n FROM analysis_backend.jobs WHERE user_id=%s AND status='reserved'",
            (db.a,),
        )[0]["n"]
        == 3
    )


def test_real_parallel_idempotency_and_payload_conflict(db):
    key = uuid4()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: db.repo.reserve(db.a, key, db.request()), range(6)))
    assert sum(created for row, created in results) == 1
    assert len({row["id"] for row, created in results}) == 1
    with pytest.raises(AnalysisServiceError, match="IDEMPOTENCY_CONFLICT"):
        db.repo.reserve(
            db.a, key, db.request(request={"ticker": "MSFT", "analysis_date": "2026-01-01"})
        )


def test_claim_is_once_and_lease_fences_late_worker(db):
    job = db.reserve()
    claim = db.repo.claim(job["id"])
    with pytest.raises(AnalysisServiceError, match="WORKER_CLAIM_REJECTED"):
        db.repo.claim(job["id"])
    db.sql(
        "UPDATE analysis_backend.jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
        (job["id"],),
    )
    assert not db.repo.heartbeat(db.a, job["id"], claim["lease_token"])
    assert len(db.repo.reap()) == 1
    with pytest.raises(AnalysisServiceError, match="WORKER_LEASE_LOST"):
        db.repo.finish(
            db.a,
            job["id"],
            claim["lease_token"],
            status="success",
            report_path="fixture",
            digest="fixture",
        )
    assert db.repo.job(db.a, job["id"])["status"] == "timed_out"


def test_failed_dispatch_releases_slot_but_replay_does_not_restart(db):
    key = uuid4()
    row, _ = db.repo.reserve(db.a, key, db.request())
    db.repo.dispatch_failed(db.a, row["id"])
    replay, created = db.repo.reserve(db.a, key, db.request())
    assert not created and replay["status"] == "failed"


def test_memory_and_result_commit_atomically(db):
    job = db.reserve()
    claim = db.repo.claim(job["id"])
    wrong = MemoryDecision(
        analysis_id=job["id"],
        user_id=db.b,
        ticker="NVDA",
        analysis_date=date(2026, 1, 1),
        rating="Buy",
        decision="Wrong owner",
        created_at=datetime.now(UTC),
        evaluation_trading_days=5,
        benchmark="SPY",
    )
    with pytest.raises(AnalysisServiceError, match="MEMORY_SCOPE_MISMATCH"):
        db.repo.finish(
            db.a,
            job["id"],
            claim["lease_token"],
            status="success",
            report_path="fixture",
            digest="fixture",
            decision=wrong,
        )
    assert db.repo.job(db.a, job["id"])["status"] == "running"
    assert (
        db.sql("SELECT count(*) AS n FROM analysis_backend.memory WHERE user_id=%s", (db.a,))[0][
            "n"
        ]
        == 0
    )


def test_memory_resolution_user_scope_idempotency_and_asof(db):
    d = db.memory()
    memory = SupabaseMemoryRepository(db.repo)
    now = datetime(2026, 1, 12, 23, tzinfo=UTC)
    outcome = MemoryOutcome(
        raw_return=0.1,
        benchmark_return=0.02,
        entry_date=date(2026, 1, 2),
        exit_date=date(2026, 1, 9),
        observed_trading_days=5,
        reflection="A-private-lesson",
        resolved_at=now,
    )
    assert memory.get_context(user_id=db.a, ticker="NVDA", as_of=now) == ""
    assert memory.resolve(user_id=db.b, analysis_id=d.analysis_id, outcome=outcome) is False
    assert memory.resolve(user_id=db.a, analysis_id=d.analysis_id, outcome=outcome) is True
    assert memory.resolve(user_id=db.a, analysis_id=d.analysis_id, outcome=outcome) is False
    assert "A-private-lesson" in memory.get_context(user_id=db.a, ticker="NVDA", as_of=now)
    assert memory.get_context(user_id=db.b, ticker="NVDA", as_of=now) == ""
    assert (
        memory.get_context(user_id=db.a, ticker="NVDA", as_of=datetime(2026, 1, 10, tzinfo=UTC))
        == ""
    )


def test_evaluator_settles_only_once_and_missing_data_defers(db):
    d = db.memory()
    sessions = [date(2026, 1, day) for day in (2, 5, 6, 7, 8, 9)]
    now = datetime(2026, 1, 12, 23, tzinfo=UTC)
    db.sql(
        "UPDATE analysis_backend.memory SET next_evaluation_at=%s WHERE analysis_id=%s",
        (now, d.analysis_id),
    )

    class Prices:
        def closes(self, *args):
            return dict.fromkeys(sessions, 100.0)

    job = EvaluationJob(db.repo, prices=Prices(), calendar=lambda d, n: sessions, now=lambda: now)
    assert job.run()["evaluated"] == 1
    assert job.run()["evaluated"] == 0
    assert (
        db.sql(
            "SELECT evaluation_attempts FROM analysis_backend.memory WHERE analysis_id=%s",
            (d.analysis_id,),
        )[0]["evaluation_attempts"]
        == 1
    )


def test_evaluator_defers_missing_prices_without_fake_lesson(db):
    d = db.memory()
    now = datetime(2026, 1, 12, 23, tzinfo=UTC)
    db.sql(
        "UPDATE analysis_backend.memory SET next_evaluation_at=%s WHERE analysis_id=%s",
        (now, d.analysis_id),
    )

    class Missing:
        def closes(self, *args):
            return {}

    result = EvaluationJob(
        db.repo,
        prices=Missing(),
        calendar=lambda d, n: [date(2026, 1, i) for i in (2, 5, 6, 7, 8, 9)],
        now=lambda: now,
    ).run()
    assert result["deferred"] == 1
    row = db.sql(
        "SELECT outcome,last_evaluation_code,evaluation_token FROM analysis_backend.memory WHERE analysis_id=%s",
        (d.analysis_id,),
    )[0]
    assert row == {
        "outcome": None,
        "last_evaluation_code": "EVALUATION_PRICES_MISSING",
        "evaluation_token": None,
    }


def test_revocation_scrubs_ciphertext_but_preserves_history(db):
    job = db.reserve()
    db.repo.revoke_connection(db.a, db.connection)
    assert (
        db.sql(
            "SELECT secret_ciphertext FROM analysis_backend.connections WHERE id=%s",
            (db.connection,),
        )[0]["secret_ciphertext"]
        is None
    )
    assert db.repo.job(db.a, job["id"])["id"] == job["id"]
    with pytest.raises(AnalysisServiceError, match="CONNECTION_NOT_FOUND"):
        db.reserve()


def test_reset_investments_does_not_delete_analysis_or_memory(db):
    d = db.memory()
    with psycopg.connect(db.dsn, autocommit=True) as conn, conn.transaction():
        conn.execute("SET LOCAL ROLE authenticated")
        conn.execute("SELECT set_config('request.jwt.claim.sub',%s,true)", (str(db.a),))
        conn.execute("SELECT public.reset_my_investments(%s,'RESET_ALL_INVESTMENTS')", (uuid4(),))
    assert db.repo.job(db.a, d.analysis_id)["status"] == "success"
    assert (
        db.sql(
            "SELECT count(*) AS n FROM analysis_backend.memory WHERE analysis_id=%s",
            (d.analysis_id,),
        )[0]["n"]
        == 1
    )


def test_rate_limit_atomic_and_terminal_job_rejects_heartbeat(db):
    db.repo.throttle(db.a, "test", 2, 60)
    db.repo.throttle(db.a, "test", 2, 60)
    with pytest.raises(AnalysisServiceError, match="RATE_LIMITED"):
        db.repo.throttle(db.a, "test", 2, 60)
    job = db.reserve()
    claim = db.repo.claim(job["id"])
    db.repo.finish(db.a, job["id"], claim["lease_token"], code="PROVIDER_ACCESS_ERROR")
    assert not db.repo.heartbeat(db.a, job["id"], claim["lease_token"])


@pytest.mark.parametrize("personal", [False, True])
def test_api_engine_worker_storage_roundtrip_and_user_b_denial(db, personal, tmp_path):
    import json
    from unittest.mock import MagicMock

    import httpx
    from fastapi.testclient import TestClient

    from tests.test_analysis_service import state
    from tests.test_personal_backend import personal_settings
    from tradingagents.service.backend.api import create_app
    from tradingagents.service.backend.security import SecretVault, SupabaseAuthenticator
    from tradingagents.service.backend.storage import ReportStorage
    from tradingagents.service.backend.worker import DeferredMemory, Worker
    from tradingagents.service.engine import AnalysisEngine
    from tradingagents.service.models import AnalysisRequest, ServiceSettings

    if personal:
        db.repo = Repository(
            personal_settings(tmp_path, database_url=db.dsn, personal_user_id=db.a)
        )

    objects = {}

    def transport(request):
        if request.url.path == "/auth/v1/user":
            user = db.a if request.headers["authorization"] == "Bearer A" else db.b
            return httpx.Response(
                200, json={"id": str(user), "role": "authenticated", "is_anonymous": personal}
            )
        if request.method == "POST":
            objects[request.url.path] = request.content
            return httpx.Response(200, json={})
        assert "/object/authenticated/analysis-reports/" in request.url.path
        path = request.url.path.replace("/object/authenticated/", "/object/", 1)
        return httpx.Response(200, content=objects[path])

    class Graph:
        def __init__(self, **kwargs):
            self.memory = kwargs["memory"]

        def propagate(self, ticker, day):
            self.memory.get_past_context(ticker)
            result = state.__wrapped__()
            result["trade_date"] = day
            return result, "Overweight"

    def popen(args, **kwargs):
        job_id = __import__("uuid").UUID(args[-3])
        row = db.repo.job(db.a, job_id)
        memory = DeferredMemory(SupabaseMemoryRepository(db.repo))
        result = AnalysisEngine(
            settings=ServiceSettings.model_validate(row["model_settings"]),
            memory=memory,
            graph_factory=Graph,
        ).run(AnalysisRequest.model_validate(row["request"]), user_id=db.a, analysis_id=job_id)
        Path(args[-1]).write_text(
            json.dumps(
                {
                    "result": result.model_dump(mode="json"),
                    "decision": memory.decision.model_dump(mode="json"),
                }
            )
        )
        return type("Process", (), {"returncode": 0, "poll": lambda self: 0})()

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        storage = ReportStorage(db.repo.settings, http)
        dispatcher = MagicMock()
        app = create_app(
            repository=db.repo,
            authenticator=SupabaseAuthenticator(db.repo.settings, http),
            vault=SecretVault(db.repo.settings),
            probe=MagicMock(),
            storage=storage,
            dispatcher=dispatcher,
        )
        with TestClient(app) as api:
            body = db.request().model_dump(mode="json")
            if personal:
                body.update(auth_type="personal_chatgpt", connection_id=None)
            response = api.post(
                "/v1/analyses",
                headers={"Authorization": "Bearer A", "Idempotency-Key": str(uuid4())},
                json=body,
            )
            assert response.status_code == 202, response.text
            job_id = __import__("uuid").UUID(response.json()["id"])
            Worker(db.repo, storage, popen=popen).run(job_id)
            assert (
                api.get(f"/v1/analyses/{job_id}", headers={"Authorization": "Bearer A"}).json()[
                    "status"
                ]
                == "success"
            )
            report = api.get(f"/v1/analyses/{job_id}/report", headers={"Authorization": "Bearer A"})
            assert report.status_code == 200 and len(report.json()["sections"]) == 12
            assert report.json()["memory_saved"] is True
            assert api.get(
                f"/v1/analyses/{job_id}/report", headers={"Authorization": "Bearer B"}
            ).status_code == (403 if personal else 404)
            assert (
                db.sql(
                    "SELECT count(*) AS n FROM analysis_backend.memory WHERE analysis_id=%s",
                    (job_id,),
                )[0]["n"]
                == 1
            )


@pytest.mark.parametrize(
    "filename",
    [
        "tests/sql/portfolio_transaction_rpc_integration.sql",
        "supabase/tests/portfolio_cash_account_model_test.sql",
        "tests/sql/portfolio_cash_reversal_snapshot_integration.sql",
        "tests/sql/portfolio_reset_rpc_integration.sql",
        "tests/sql/user_watchlist_rpc_test.sql",
        "tests/sql/portfolio_activity_rpc_test.sql",
        "tests/sql/mobile_system_health_rpc_test.sql",
        "tests/sql/personal_analysis_auth_test.sql",
    ],
)
def test_existing_finbot_sql_contracts_still_pass(cluster, filename):
    root = Path(os.environ.get("FINBOT_ROOT", str(Path(__file__).resolve().parents[2] / "FinBot")))
    sql = (root / filename).read_text().replace("\\set ON_ERROR_STOP on", "")
    with psycopg.connect(cluster, autocommit=True) as conn:
        conn.execute(sql, prepare=False)


def test_orphan_metadata_query_excludes_referenced_fresh_and_other_buckets(db):
    from datetime import timedelta

    db.memory()  # report_path='fixture' is committed and must be preserved
    old = datetime(2026, 1, 1, tzinfo=UTC)
    now = datetime.now(UTC)
    orphan = f"{db.a}/{uuid4()}/{uuid4()}.json"
    names = [
        ("fixture", "analysis-reports", old),
        (orphan, "analysis-reports", old),
        ("fresh", "analysis-reports", now),
        ("foreign", "other-bucket", old),
    ]
    ids = []
    try:
        for name, bucket, created in names:
            row = db.sql(
                "INSERT INTO storage.objects(bucket_id,name,created_at) VALUES(%s,%s,%s) RETURNING id",
                (bucket, name, created),
            )[0]
            ids.append(row["id"])
        assert db.repo.orphan_reports(now - timedelta(hours=24)) == [{"name": orphan}]
        # Even an unrelated existing PUBLIC storage policy cannot widen our role.
        db.sql("CREATE POLICY test_public_metadata ON storage.objects FOR SELECT USING (true)")
        with db.repo.transaction() as conn:
            assert (
                conn.execute(
                    "SELECT name FROM storage.objects WHERE bucket_id='other-bucket'"
                ).fetchall()
                == []
            )
    finally:
        db.sql("DROP POLICY IF EXISTS test_public_metadata ON storage.objects")
        db.sql("DELETE FROM storage.objects WHERE id=ANY(%s)", (ids,))
