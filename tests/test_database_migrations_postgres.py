"""Real PostgreSQL migration acceptance, using ONLY a disposable loopback DB.

Minimal auth/storage/catalog stubs model the three copied SQL files' platform
dependencies. They do not replace FinBot's full Supabase acceptance suite.
"""

import os
import shutil
import socket
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from deploy.personal import migrate
from tests.test_database_migrations import SOURCE, add_file, bundle

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    supplied = os.environ.get("TEST_MIGRATION_DATABASE_URL")
    if supplied:
        info = conninfo_to_dict(supplied)
        assert info.get("host") in {"127.0.0.1", "localhost", "::1"}, "Disposable loopback DB only"
        assert not info.get("hostaddr") or info["hostaddr"] in {"127.0.0.1", "::1"}
        yield supplied
        return
    assert not os.environ.get("CI"), "CI must supply its disposable PostgreSQL service"
    binaries = Path(os.environ.get("TEST_PG_BIN", "/opt/homebrew/opt/postgresql@17/bin"))
    if not (binaries / "initdb").is_file():
        executable = shutil.which("initdb")
        if not executable:
            pytest.skip("Set TEST_PG_BIN or TEST_MIGRATION_DATABASE_URL for disposable PostgreSQL")
        binaries = Path(executable).parent
    root = tmp_path_factory.mktemp("migration-postgres")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    subprocess.run([str(binaries / "initdb"), "-D", str(root / "db"), "-U", "postgres",
                    "-A", "trust", "--no-locale", "-E", "UTF8"], check=True, capture_output=True)
    subprocess.run([str(binaries / "pg_ctl"), "-D", str(root / "db"), "-l", str(root / "pg.log"),
                    "-o", f"-h 127.0.0.1 -p {port} -c unix_socket_directories=''", "-w", "start"],
                   check=True, capture_output=True)
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run([str(binaries / "pg_ctl"), "-D", str(root / "db"), "-m", "fast", "-w", "stop"],
                       check=True, capture_output=True)


@pytest.fixture
def database(cluster):
    name = "migration_" + uuid4().hex
    with psycopg.connect(cluster, autocommit=True) as admin:
        for role in ("anon", "authenticated", "service_role"):
            if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    dsn = make_conninfo(cluster, dbname=name)
    try:
        with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as conn:
            conn.execute("""CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY);
                CREATE SCHEMA storage;
                CREATE TABLE storage.buckets(id text PRIMARY KEY,name text,public boolean,
                                            file_size_limit bigint,allowed_mime_types text[]);
                CREATE TABLE storage.objects(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                    bucket_id text,name text,created_at timestamptz DEFAULT now());
                ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;
                CREATE TABLE public.instruments(id uuid PRIMARY KEY,market text,asset_type text);
                ALTER TABLE public.instruments ENABLE ROW LEVEL SECURITY;
                CREATE SCHEMA supabase_migrations;
                CREATE TABLE supabase_migrations.schema_migrations (
                    version text PRIMARY KEY,statements text[],name text);""", prepare=False)
            for m in migrate.load(SOURCE):
                if not m.baseline:
                    continue
                conn.execute((SOURCE / m.file).read_text(), prepare=False)
                conn.execute(f"INSERT INTO {migrate.HISTORY}(version,name,statements) VALUES (%s,%s,%s)",
                             (m.version, m.name, list(m.statements)))
            yield conn, dsn
    finally:
        with psycopg.connect(cluster, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_historical_baseline_is_checked_read_only_and_adopted_once(database):
    conn, _ = database
    files = [m for m in migrate.load(SOURCE) if m.baseline]
    result = migrate.run(conn, files)
    assert result == {"status": "checked", "baseline_verified": 3, "pending": [], "applied": 0}
    assert conn.execute("SELECT to_regclass(%s)", (migrate.LEDGER,)).fetchone()[0] is None
    assert migrate.run(conn, files, apply=True)["applied"] == 0
    assert migrate.run(conn, files, apply=True)["applied"] == 0
    assert conn.execute(f"SELECT count(*) FROM {migrate.LEDGER}").fetchone()[0] == 3
    assert conn.execute(f"SELECT count(*) FROM {migrate.HISTORY}").fetchone()[0] == 3
    for role in ("anon", "authenticated", "lucrari_analysis_backend"):
        assert not conn.execute("SELECT has_schema_privilege(%s,'tradingagents_deploy','USAGE')", (role,)).fetchone()[0]


def test_missing_or_renamed_baseline_stops_without_creating_ledger(database):
    conn, _ = database
    conn.execute(f"DELETE FROM {migrate.HISTORY} WHERE version='20261024000000'")
    with pytest.raises(ValueError, match="baseline"):
        migrate.run(conn, migrate.load(SOURCE), apply=True)
    assert conn.execute("SELECT to_regclass(%s)", (migrate.LEDGER,)).fetchone()[0] is None


def test_new_column_history_and_checksum_commit_together_and_reruns_skip(database, tmp_path):
    conn, _ = database
    files = add_file(bundle(tmp_path), "BEGIN; ALTER TABLE analysis_backend.jobs ADD COLUMN note text; COMMIT;")
    assert migrate.run(conn, files)["pending"] == ["20261030000000"]
    assert migrate.run(conn, files, apply=True)["applied"] == 1
    assert conn.execute("SELECT note,status FROM analysis_backend.jobs").fetchall() == []
    row = conn.execute(f"SELECT name,sha256 FROM {migrate.LEDGER} WHERE version=%s", (files[-1].version,)).fetchone()
    assert row == (files[-1].name, files[-1].checksum)
    assert conn.execute(f"SELECT name FROM {migrate.HISTORY} WHERE version=%s", (files[-1].version,)).fetchone()[0] == files[-1].name
    assert migrate.run(conn, files, apply=True)["applied"] == 0


def test_later_sql_error_rolls_back_whole_batch_and_all_metadata(database, tmp_path):
    conn, _ = database
    root = bundle(tmp_path)
    add_file(root, "ALTER TABLE analysis_backend.jobs ADD COLUMN note text;")
    files = add_file(root, "ALTER TABLE analysis_backend.jobs ADD COLUMN status text;", "20261030000001_invalid_column.sql")
    with pytest.raises(psycopg.errors.DuplicateColumn):
        migrate.run(conn, files, apply=True)
    assert conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='analysis_backend' AND table_name='jobs' AND column_name='note'").fetchone() is None
    assert conn.execute(f"SELECT count(*) FROM {migrate.HISTORY}").fetchone()[0] == 3
    assert conn.execute("SELECT to_regclass(%s)", (migrate.LEDGER,)).fetchone()[0] is None


def test_concurrent_migration_fails_without_waiting_or_changing_database(database):
    conn, dsn = database
    with psycopg.connect(dsn, autocommit=True) as other, other.transaction():
        other.execute("SELECT pg_advisory_xact_lock(%s)", (migrate.LOCK_ID,))
        with pytest.raises(ValueError, match="Another"):
            migrate.run(conn, migrate.load(SOURCE), apply=True)
    assert conn.execute("SELECT to_regclass(%s)", (migrate.LEDGER,)).fetchone()[0] is None


def test_new_table_without_rls_is_rolled_back(database, tmp_path):
    conn, _ = database
    files = add_file(bundle(tmp_path), "CREATE TABLE analysis_backend.extra(id uuid PRIMARY KEY);")
    with pytest.raises(ValueError, match="RLS"):
        migrate.run(conn, files, apply=True)
    assert conn.execute("SELECT to_regclass('analysis_backend.extra')").fetchone()[0] is None


def test_new_table_requires_backend_only_policy_and_grants(database, tmp_path):
    conn, _ = database
    files = add_file(bundle(tmp_path), """CREATE TABLE analysis_backend.extra(id uuid PRIMARY KEY);
        ALTER TABLE analysis_backend.extra ENABLE ROW LEVEL SECURITY;
        CREATE POLICY backend_extra ON analysis_backend.extra TO lucrari_analysis_backend USING (true) WITH CHECK (true);
        GRANT SELECT,INSERT,UPDATE,DELETE ON analysis_backend.extra TO lucrari_analysis_backend;""")
    assert migrate.run(conn, files, apply=True)["applied"] == 1
    with conn.transaction():
        conn.execute("SET LOCAL ROLE lucrari_analysis_backend")
        conn.execute("INSERT INTO analysis_backend.extra VALUES (gen_random_uuid())")
        assert conn.execute("SELECT count(*) FROM analysis_backend.extra").fetchone()[0] == 1
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("SET LOCAL ROLE authenticated")
        conn.execute("SELECT * FROM analysis_backend.extra")


def test_history_drift_and_changed_applied_checksum_block_next_deploy(database):
    conn, _ = database
    files = migrate.load(SOURCE)
    migrate.run(conn, files, apply=True)
    conn.execute(f"UPDATE {migrate.LEDGER} SET sha256=%s WHERE version=%s", ("0" * 64, files[0].version))
    with pytest.raises(ValueError, match="changed"):
        migrate.run(conn, files, apply=True)


def test_actual_production_bundle_applies_cleanly_on_historical_baseline(database):
    conn, _ = database
    files = migrate.load(SOURCE)
    expected = sum(not m.baseline for m in files)
    assert migrate.run(conn, files, apply=True)["applied"] == expected
    assert migrate.run(conn, files, apply=True)["applied"] == 0
    assert conn.execute(f"SELECT count(*) FROM {migrate.LEDGER}").fetchone()[0] == len(files)
