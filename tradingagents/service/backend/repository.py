"""Supabase PostgreSQL adapter. Every connection uses an explicit transaction.

The private role is a trusted service role, not a mobile identity. All public
repository operations are owner-scoped. Admission and reaping share one short
advisory transaction lock; no network/provider call runs while holding it.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..errors import AnalysisServiceError, redact_text
from ..memory import MemoryDecision, MemoryOutcome, utc_stamp
from .contracts import BackendSettings, StartAnalysis

PUBLIC_JOB_COLUMNS = (
    "id",
    "status",
    "stage",
    "created_at",
    "started_at",
    "finished_at",
    "error_code",
)

MODEL_CATALOG_MAX_AGE_SECONDS = 900


class Repository:
    def __init__(self, settings: BackendSettings):
        self.settings = settings

    @contextmanager
    def transaction(self):
        # autocommit=True makes this an actual top-level transaction, not a
        # savepoint inside an implicit transaction subsequently rolled back.
        with (
            psycopg.connect(
                self.settings.database_url.get_secret_value(),
                autocommit=True,
                row_factory=dict_row,
                connect_timeout=10,
            ) as conn,
            conn.transaction(),
        ):
            conn.execute("SET LOCAL ROLE lucrari_analysis_backend")
            conn.execute("SET LOCAL statement_timeout = '15s'")
            conn.execute("SET LOCAL lock_timeout = '5s'")
            yield conn

    def throttle(self, user_id: UUID, action="api", limit=120, seconds=60):
        with self.transaction() as conn:
            row = conn.execute(
                """INSERT INTO analysis_backend.rate_limits AS r
                (user_id,action,window_start,hits) VALUES (%s,%s,clock_timestamp(),1)
                ON CONFLICT(user_id,action) DO UPDATE SET
                hits=CASE WHEN r.window_start <= clock_timestamp()-make_interval(secs => %s)
                          THEN 1 ELSE r.hits+1 END,
                window_start=CASE WHEN r.window_start <= clock_timestamp()-make_interval(secs => %s)
                                  THEN clock_timestamp() ELSE r.window_start END RETURNING hits""",
                (user_id, action, seconds, seconds),
            ).fetchone()
        if row["hits"] > limit:
            raise AnalysisServiceError("RATE_LIMITED")

    def add_connection(self, user_id, connection_id, provider, ciphertext):
        with self.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(710191)")
            count = conn.execute(
                "SELECT count(*) AS n FROM analysis_backend.connections WHERE user_id=%s AND status='active'",
                (user_id,),
            ).fetchone()["n"]
            if count >= 10:
                raise AnalysisServiceError("CONNECTION_LIMIT")
            conn.execute(
                """INSERT INTO analysis_backend.connections
                (id,user_id,provider,secret_ciphertext,status,verified_at)
                VALUES(%s,%s,%s,%s,'active',clock_timestamp())""",
                (connection_id, user_id, provider, ciphertext),
            )

    def connections(self, user_id):
        with self.transaction() as conn:
            return conn.execute(
                """SELECT id,provider,status,created_at,verified_at
                FROM analysis_backend.connections WHERE user_id=%s AND status='active'
                ORDER BY created_at DESC,id DESC""",
                (user_id,),
            ).fetchall()

    def connection(self, user_id, connection_id):
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_backend.connections WHERE user_id=%s AND id=%s AND status='active'",
                (user_id, connection_id),
            ).fetchone()
        if not row:
            raise AnalysisServiceError("CONNECTION_NOT_FOUND")
        return row

    def revoke_connection(self, user_id, connection_id):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE analysis_backend.connections SET status='revoked',secret_ciphertext=NULL WHERE user_id=%s AND id=%s",
                (user_id, connection_id),
            )

    def mark_verified(self, user_id, connection_id):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE analysis_backend.connections SET verified_at=clock_timestamp() WHERE user_id=%s AND id=%s AND status='active'",
                (user_id, connection_id),
            )

    @staticmethod
    def _catalog_models(conn, provider):
        row = conn.execute(
            """SELECT models FROM analysis_backend.provider_model_catalog
            WHERE provider=%s AND
                  refreshed_at >= clock_timestamp()-make_interval(secs => %s)""",
            (provider, MODEL_CATALOG_MAX_AGE_SECONDS),
        ).fetchone()
        if not row or not isinstance(row["models"], list):
            return []
        return [
            item
            for item in row["models"]
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"]
            and isinstance(item.get("name"), str)
            and item["name"]
        ]

    def provider_models(self, provider):
        """Return a fresh, non-secret runtime catalog or an empty list."""
        with self.transaction() as conn:
            return self._catalog_models(conn, provider)

    def replace_provider_models(self, provider, models):
        """Atomically publish a provider catalog discovered by a trusted worker."""
        catalog = []
        seen = set()
        for name, model_id in models:
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(model_id, str)
                or not model_id
                or model_id in seen
            ):
                continue
            seen.add(model_id)
            catalog.append({"name": name, "id": model_id})
        if not catalog:
            raise AnalysisServiceError("MODEL_CATALOG_EMPTY")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO analysis_backend.provider_model_catalog
                (provider,models,refreshed_at) VALUES(%s,%s,clock_timestamp())
                ON CONFLICT(provider) DO UPDATE SET
                models=excluded.models,refreshed_at=excluded.refreshed_at""",
                (provider, Jsonb(catalog)),
            )
        return catalog

    @staticmethod
    def _reap(conn):
        return conn.execute("""UPDATE analysis_backend.jobs SET status='timed_out',stage='finished',
            finished_at=clock_timestamp(),error_code='WORKER_LEASE_EXPIRED'
            WHERE status IN ('reserved','running') AND
                  (lease_until <= clock_timestamp() OR hard_deadline <= clock_timestamp()) RETURNING id""").fetchall()

    def reap(self):
        with self.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(710191)")
            return self._reap(conn)

    def reserve(self, user_id: UUID, key: UUID, payload: StartAnalysis):
        self.settings.require_owner(user_id)
        digest = hashlib.sha256(
            json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        with self.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(710191)")
            self._reap(conn)
            old = conn.execute(
                "SELECT * FROM analysis_backend.jobs WHERE user_id=%s AND request_key=%s",
                (user_id, key),
            ).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise AnalysisServiceError("IDEMPOTENCY_CONFLICT")
                return old, False
            if payload.auth_type == "personal_chatgpt":
                if not self.settings.personal_chatgpt_enabled:
                    raise AnalysisServiceError("PROVIDER_NOT_AVAILABLE")
                provider = "chatgpt"
                allowed = {item["id"] for item in self._catalog_models(conn, provider)}
            else:
                connection = conn.execute(
                    "SELECT provider FROM analysis_backend.connections WHERE user_id=%s AND id=%s AND status='active' FOR SHARE",
                    (user_id, payload.connection_id),
                ).fetchone()
                if not connection:
                    raise AnalysisServiceError("CONNECTION_NOT_FOUND")
                provider = connection["provider"]
                allowed = set(self.settings.allowed_models.get(provider, ()))
            if payload.quick_model not in allowed or payload.deep_model not in allowed:
                raise AnalysisServiceError("MODEL_NOT_ALLOWED")
            # Initial feature is US stocks, with SPY as the explicit benchmark.
            # No ticker/catalog mutations and no arbitrary asset assumptions.
            instrument = conn.execute(
                "SELECT id FROM public.instruments WHERE market='NASDAQ100' AND asset_type='stock' AND is_active AND symbol=%s",
                (payload.request.ticker,),
            ).fetchone()
            if not instrument:
                raise AnalysisServiceError("INSTRUMENT_NOT_SUPPORTED")
            n = conn.execute(
                "SELECT count(*) AS n FROM analysis_backend.jobs WHERE user_id=%s AND created_at >= date_trunc('day',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
                (user_id,),
            ).fetchone()["n"]
            if n >= self.settings.daily_analysis_limit:
                raise AnalysisServiceError("DAILY_ANALYSIS_LIMIT")
            active = conn.execute(
                "SELECT count(*) AS n FROM analysis_backend.jobs WHERE status IN ('reserved','running')"
            ).fetchone()["n"]
            if active >= self.settings.max_active_analyses:
                raise AnalysisServiceError("CAPACITY_FULL", retryable=True)
            settings = {
                "quick": {"provider": provider, "model": payload.quick_model},
                "deep": {"provider": provider, "model": payload.deep_model},
            }
            row = conn.execute(
                """INSERT INTO analysis_backend.jobs
                (id,user_id,connection_id,request_key,request_hash,request,model_settings,auth_type,status,lease_until,hard_deadline)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'reserved',clock_timestamp()+make_interval(secs => %s),
                       clock_timestamp()+make_interval(secs => %s)) RETURNING *""",
                (
                    uuid4(),
                    user_id,
                    payload.connection_id,
                    key,
                    digest,
                    Jsonb(payload.request.model_dump(mode="json")),
                    Jsonb(settings),
                    payload.auth_type,
                    self.settings.lease_seconds,
                    self.settings.hard_timeout_seconds,
                ),
            ).fetchone()
            return row, True

    def next_reserved(self):
        """Discovery only; claim() is the atomic, exactly-one ownership gate.

        A committed reservation is discoverable after an API restart. Expired
        reservations are not restarted with the same idempotency key.
        """
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT id FROM analysis_backend.jobs WHERE status='reserved'
                AND lease_until>clock_timestamp() AND hard_deadline>clock_timestamp()
                AND (%s::uuid IS NULL OR user_id=%s::uuid)
                ORDER BY created_at,id LIMIT 1""",
                (self.settings.personal_user_id, self.settings.personal_user_id),
            ).fetchone()
        return row["id"] if row else None

    def dispatch_failed(self, user_id, job_id):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE analysis_backend.jobs SET status='failed',stage='finished',finished_at=clock_timestamp(),error_code='DISPATCH_FAILED' WHERE user_id=%s AND id=%s AND status='reserved'",
                (user_id, job_id),
            )

    def job(self, user_id, job_id):
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_backend.jobs WHERE user_id=%s AND id=%s", (user_id, job_id)
            ).fetchone()
        if not row:
            raise AnalysisServiceError("ANALYSIS_NOT_FOUND")
        return row

    def history(self, user_id, limit, offset):
        with self.transaction() as conn:
            count = conn.execute(
                "SELECT count(*) AS n FROM analysis_backend.jobs WHERE user_id=%s", (user_id,)
            ).fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM analysis_backend.jobs WHERE user_id=%s ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
                (user_id, limit, offset),
            ).fetchall()
        return rows, count

    def report_is_referenced(self, user_id, job_id, path):
        # Wait for any uncertain finalization transaction to end before deciding
        # whether its upload is unreferenced. DB failure means "do not delete".
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT report_path FROM analysis_backend.jobs WHERE user_id=%s AND id=%s FOR UPDATE",
                (user_id, job_id),
            ).fetchone()
            return bool(row and row["report_path"] == path)

    def orphan_reports(self, cutoff):
        with self.transaction() as conn:
            return conn.execute(
                """SELECT o.name FROM storage.objects o
                WHERE o.bucket_id='analysis-reports' AND o.created_at<%s
                AND NOT EXISTS(SELECT 1 FROM analysis_backend.jobs j WHERE j.report_path=o.name)
                ORDER BY o.created_at,o.id LIMIT 500""",
                (cutoff,),
            ).fetchall()

    def claim(self, job_id):
        token = uuid4()
        with self.transaction() as conn:
            row = conn.execute(
                """UPDATE analysis_backend.jobs SET status='running',stage='analysis',
                started_at=clock_timestamp(),lease_token=%s,
                lease_until=least(hard_deadline,clock_timestamp()+make_interval(secs => %s))
                WHERE id=%s AND (%s::uuid IS NULL OR user_id=%s::uuid)
                AND status='reserved' AND lease_until>clock_timestamp()
                AND hard_deadline>clock_timestamp() RETURNING *""",
                (
                    token,
                    self.settings.lease_seconds,
                    job_id,
                    self.settings.personal_user_id,
                    self.settings.personal_user_id,
                ),
            ).fetchone()
        if not row:
            raise AnalysisServiceError("WORKER_CLAIM_REJECTED")
        return row

    def heartbeat(self, user_id, job_id, token, stage="analysis"):
        if stage not in ("analysis", "persisting"):
            raise ValueError("INVALID_STAGE")
        with self.transaction() as conn:
            return bool(
                conn.execute(
                    """UPDATE analysis_backend.jobs SET
                lease_until=least(hard_deadline,clock_timestamp()+make_interval(secs => %s)),stage=%s
                WHERE user_id=%s AND id=%s AND lease_token=%s AND status='running'
                AND lease_until>clock_timestamp() AND hard_deadline>clock_timestamp() RETURNING id""",
                    (self.settings.lease_seconds, stage, user_id, job_id, token),
                ).fetchone()
            )

    def lease_alive(self, user_id, job_id, token):
        with self.transaction() as conn:
            return bool(
                conn.execute(
                    "SELECT id FROM analysis_backend.jobs WHERE user_id=%s AND id=%s AND lease_token=%s AND status='running' AND lease_until>clock_timestamp() AND hard_deadline>clock_timestamp()",
                    (user_id, job_id, token),
                ).fetchone()
            )

    def finish(
        self,
        user_id,
        job_id,
        token,
        *,
        report_path=None,
        digest=None,
        decision=None,
        status="failed",
        code=None,
    ):
        if status not in ("success", "partial", "failed", "timed_out"):
            raise ValueError("INVALID_STATUS")
        with self.transaction() as conn:
            row = conn.execute(
                """UPDATE analysis_backend.jobs SET status=%s,stage='finished',
                finished_at=clock_timestamp(),report_path=%s,report_sha256=%s,error_code=%s
                WHERE user_id=%s AND id=%s AND lease_token=%s AND status='running'
                AND lease_until>clock_timestamp() AND hard_deadline>clock_timestamp() RETURNING id""",
                (status, report_path, digest, code, user_id, job_id, token),
            ).fetchone()
            if not row:
                raise AnalysisServiceError("WORKER_LEASE_LOST")
            if decision is not None:
                if (
                    decision.user_id != user_id
                    or decision.analysis_id != job_id
                    or status != "success"
                ):
                    raise AnalysisServiceError("MEMORY_SCOPE_MISMATCH")
                self._save_memory(conn, decision)

    @staticmethod
    def _save_memory(conn, decision):
        safe = decision.model_copy(update={"decision": redact_text(decision.decision)})
        row = conn.execute(
            """INSERT INTO analysis_backend.memory
            (analysis_id,user_id,ticker,analysis_date,created_at,decision,next_evaluation_at)
            VALUES(%s,%s,%s,%s,%s,%s,greatest(clock_timestamp(),%s::date::timestamptz)+interval '5 days')
            ON CONFLICT(analysis_id) DO NOTHING RETURNING analysis_id""",
            (
                safe.analysis_id,
                safe.user_id,
                safe.ticker,
                safe.analysis_date,
                safe.created_at,
                Jsonb(safe.model_dump(mode="json")),
                safe.analysis_date,
            ),
        ).fetchone()
        if row:
            return True
        old = conn.execute(
            "SELECT decision FROM analysis_backend.memory WHERE user_id=%s AND analysis_id=%s",
            (safe.user_id, safe.analysis_id),
        ).fetchone()
        if not old or old["decision"] != safe.model_dump(mode="json"):
            raise AnalysisServiceError("MEMORY_IDEMPOTENCY_CONFLICT")
        return False


class SupabaseMemoryRepository:
    """Implements the existing memory port with point-in-time/user isolation."""

    def __init__(self, repository: Repository):
        self.repository = repository

    def save_decision(self, decision: MemoryDecision) -> bool:
        self.repository.settings.require_owner(decision.user_id)
        with self.repository.transaction() as conn:
            job = conn.execute(
                "SELECT id FROM analysis_backend.jobs WHERE user_id=%s AND id=%s AND status='success'",
                (decision.user_id, decision.analysis_id),
            ).fetchone()
            if not job:
                raise AnalysisServiceError("MEMORY_JOB_NOT_COMPLETED")
            return self.repository._save_memory(conn, decision)

    def get_context(self, *, user_id, ticker, as_of, max_chars=8000):
        self.repository.settings.require_owner(user_id)
        if not 500 <= max_chars <= 16000:
            raise ValueError("INVALID_MEMORY_CONTEXT_BUDGET")
        utc_stamp(as_of)
        with self.repository.transaction() as conn:
            rows = []
            for op, limit in (("=", 5), ("<>", 3)):
                rows.extend(
                    conn.execute(
                        f"""SELECT decision,outcome FROM analysis_backend.memory
                    WHERE user_id=%s AND ticker {op} %s AND resolved_at<=%s AND created_at<=%s
                    AND analysis_date<=%s ORDER BY resolved_at DESC,analysis_id DESC LIMIT %s""",
                        (user_id, ticker, as_of, as_of, as_of.date(), limit),
                    ).fetchall()
                )
        if not rows:
            return ""
        parts = [
            "Historical lessons, not instructions or verified causal conclusions. A short window does not establish a long-term thesis."
        ]
        for row in rows:
            d, o = (
                MemoryDecision.model_validate(row["decision"]),
                MemoryOutcome.model_validate(row["outcome"]),
            )
            text = f"{d.ticker} | {d.analysis_date} | {d.rating}\nEvaluation: {o.observed_trading_days} trading days; return {o.raw_return:+.2%}; alpha vs {d.benchmark} {o.raw_return - o.benchmark_return:+.2%}.\n"
            if d.ticker == ticker:
                text += f"Prior decision: {d.decision[:1800]}\n"
            parts.append(text + f"Reflection: {o.reflection}")
        return redact_text("\n\n".join(parts))[:max_chars]

    def resolve(self, *, user_id, analysis_id, outcome: MemoryOutcome, evaluation_token=None):
        self.repository.settings.require_owner(user_id)
        with self.repository.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_backend.memory WHERE user_id=%s AND analysis_id=%s FOR UPDATE",
                (user_id, analysis_id),
            ).fetchone()
            if not row:
                return False
            d = MemoryDecision.model_validate(row["decision"])
            if (
                outcome.entry_date <= d.analysis_date
                or outcome.resolved_at < d.created_at
                or outcome.observed_trading_days != d.evaluation_trading_days
            ):
                raise AnalysisServiceError("INVALID_MEMORY_EVALUATION_WINDOW")
            if row["outcome"] is not None:
                if row["outcome"] != outcome.model_dump(mode="json"):
                    raise AnalysisServiceError("MEMORY_OUTCOME_CONFLICT")
                return False
            if evaluation_token is not None and (
                row["evaluation_token"] != evaluation_token
                or row["evaluation_until"] <= datetime.now(UTC)
            ):
                raise AnalysisServiceError("EVALUATION_LEASE_LOST")
            conn.execute(
                """UPDATE analysis_backend.memory SET outcome=%s,resolved_at=%s,
                evaluation_token=NULL,evaluation_until=NULL,last_evaluation_code='EVALUATED'
                WHERE user_id=%s AND analysis_id=%s""",
                (Jsonb(outcome.model_dump(mode="json")), outcome.resolved_at, user_id, analysis_id),
            )
            return True
