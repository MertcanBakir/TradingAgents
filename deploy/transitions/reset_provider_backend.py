"""Reviewed one-off schema-v2 reset. Dry-run by default; never run from CD.

Only analysis_backend data and validated analysis-reports objects are touched.
Run with admissions blocked and no active workers. Storage deletion cannot be
rolled back by PostgreSQL: failures require a fresh plan and operator review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import httpx
import psycopg
from pglast import split
from psycopg.conninfo import conninfo_to_dict

from tradingagents.service.backend.runtime import settings_from_env
from tradingagents.service.backend.storage import ReportStorage

SQL_FILE = Path(__file__).with_name("20261102000000_multi_user_provider_connections.sql")
VERSION = "20261102000000"
NAME = "multi_user_provider_connections"
CONFIRMATION = "reset-analysis-backend-v2"
BASELINE = {
    "20261019000000": "analysis_backend",
    "20261022000000": "personal_analysis_auth",
    "20261024000000": "dynamic_chatgpt_model_catalog",
}


def snapshot(conn, settings):
    if conn.execute("SELECT to_regclass('analysis_backend.schema_revision')").fetchone()[0]:
        raise ValueError("Transition already applied")
    history = dict(
        conn.execute("SELECT version,name FROM supabase_migrations.schema_migrations").fetchall()
    )
    if any(history.get(v) != n for v, n in BASELINE.items()) or VERSION in history:
        raise ValueError("Unexpected migration history")
    active = conn.execute(
        "SELECT count(*) FROM analysis_backend.jobs WHERE status IN ('reserved','running')"
    ).fetchone()[0]
    if active:
        raise ValueError("Active analyses must be drained before reset")
    rows = conn.execute("SELECT id,report_path FROM analysis_backend.jobs ORDER BY id").fetchall()
    paths = {p for _, p in rows if p}
    paths.update(
        p
        for (p,) in conn.execute(
            "SELECT name FROM storage.objects WHERE bucket_id='analysis-reports'"
        )
    )
    validator = ReportStorage(settings, None)
    for path in paths:
        validator._url(path)  # Validate all paths before the first deletion.
    counts = {
        table: conn.execute(f"SELECT count(*) FROM analysis_backend.{table}").fetchone()[0]
        for table in ("jobs", "memory", "connections", "rate_limits")
    }
    info = conninfo_to_dict(settings.database_url.get_secret_value())
    identity = {key: info.get(key) for key in ("host", "port", "dbname", "user")}
    identity["supabase_url"] = settings.supabase_url
    material = {
        "database": identity,
        "jobs": [str(j) for j, _ in rows],
        "paths": sorted(paths),
        "counts": counts,
        "connection_ids": [
            str(x[0])
            for x in conn.execute("SELECT id FROM analysis_backend.connections ORDER BY id")
        ],
        "sql_sha256": hashlib.sha256(SQL_FILE.read_bytes()).hexdigest(),
    }
    plan_hash = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    return {"plan_hash": plan_hash, "counts": counts, "report_objects": len(paths)}, sorted(paths)


def transition(
    conn, settings, storage, *, apply=False, plan_hash=None, confirmation=None, quiesced=False
):
    with conn.transaction():
        if not apply:
            conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL lock_timeout='5s'")
        conn.execute("SET LOCAL statement_timeout='120s'")
        for lock in (710191, 710195):
            if not conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock,)).fetchone()[0]:
                raise ValueError("Backend or migration operation is in progress")
        if apply:
            if confirmation != CONFIRMATION or not plan_hash or not quiesced:
                raise ValueError(
                    "Explicit reset confirmation, quiesced services and plan hash required"
                )
            conn.execute(
                "LOCK TABLE analysis_backend.jobs,analysis_backend.memory,analysis_backend.connections,analysis_backend.rate_limits IN ACCESS EXCLUSIVE MODE"
            )
            conn.execute(
                "LOCK TABLE supabase_migrations.schema_migrations IN SHARE ROW EXCLUSIVE MODE"
            )
        plan, paths = snapshot(conn, settings)
        if not apply:
            return {"status": "planned", **plan}
        if plan["plan_hash"] != plan_hash:
            raise ValueError("Reset plan changed; inspect a fresh dry-run")
        # Never delete storage.objects via SQL. Network deletions are deliberately
        # completed before SQL data/structure changes. No secrets in output.
        for path in paths:
            if not storage.discard(path):
                raise ValueError("Report deletion failed; inspect and create a new plan")
        parts = list(split(SQL_FILE.read_text()))
        assert parts[0].strip().upper() == "BEGIN" and parts[-1].strip().upper() == "COMMIT"
        for statement in parts[1:-1]:
            conn.execute(statement, prepare=False)
        conn.execute(
            "INSERT INTO supabase_migrations.schema_migrations(version,name,statements) VALUES (%s,%s,%s)",
            (VERSION, NAME, parts[1:-1]),
        )
        return {"status": "transitioned", "schema_revision": 2, **plan}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--plan-hash")
    parser.add_argument("--services-quiesced", action="store_true")
    args = parser.parse_args()
    try:
        settings = settings_from_env()
        info = conninfo_to_dict(settings.database_url.get_secret_value())
        if (
            info.get("host") not in {"localhost", "127.0.0.1", "::1"}
            and info.get("sslmode") != "verify-full"
        ):
            raise ValueError("Verified TLS required")
        with (
            psycopg.connect(
                settings.database_url.get_secret_value(),
                autocommit=True,
                connect_timeout=10,
                prepare_threshold=None,
            ) as conn,
            httpx.Client(trust_env=False) as client,
        ):
            result = transition(
                conn,
                settings,
                ReportStorage(settings, client),
                apply=args.apply,
                confirmation=args.confirm,
                plan_hash=args.plan_hash,
                quiesced=args.services_quiesced,
            )
            print(json.dumps(result))
    except Exception:
        print(json.dumps({"status": "failed", "code": "BACKEND_TRANSITION_FAILED"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
