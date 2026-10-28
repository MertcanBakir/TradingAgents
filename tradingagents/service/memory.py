"""User-scoped memory port and a durable local SQLite adapter.

No market or LLM calls happen here. A later Supabase implementation can implement
the same port; settlement is explicit and never runs inside an analysis request.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .errors import AnalysisServiceError, redact_text
from .models import Contract, Rating, validate_ticker


def utc_stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class MemoryDecision(Contract):
    analysis_id: UUID
    user_id: UUID
    ticker: str
    analysis_date: date
    rating: Rating
    decision: str = Field(min_length=1, max_length=40000)
    created_at: datetime
    evaluation_trading_days: int = Field(ge=1, le=252, strict=True)
    benchmark: str

    _ticker = field_validator("ticker")(validate_ticker)
    _benchmark = field_validator("benchmark")(validate_ticker)

    @field_validator("created_at")
    @classmethod
    def aware_created_at(cls, value):
        utc_stamp(value)
        return value.astimezone(UTC)

    @field_validator("decision")
    @classmethod
    def nonempty_decision(cls, value):
        if not value.strip():
            raise ValueError("EMPTY_MEMORY_DECISION")
        return value.strip()

    @field_validator("rating")
    @classmethod
    def actionable_rating(cls, value):
        if value == "REVIEW":
            raise ValueError("REVIEW_IS_NOT_A_MEMORY_DECISION")
        return value


class MemoryOutcome(Contract):
    raw_return: float = Field(ge=-1, allow_inf_nan=False)
    benchmark_return: float = Field(ge=-1, allow_inf_nan=False)
    entry_date: date
    exit_date: date
    observed_trading_days: int = Field(ge=1, le=252, strict=True)
    reflection: str = Field(min_length=1, max_length=2000)
    resolved_at: datetime

    @field_validator("reflection")
    @classmethod
    def nonempty_reflection(cls, value):
        if not value.strip():
            raise ValueError("EMPTY_REFLECTION")
        return value.strip()

    @model_validator(mode="after")
    def dates_are_consistent(self):
        utc_stamp(self.resolved_at)
        if self.resolved_at > datetime.now(UTC):
            raise ValueError("FUTURE_RESOLUTION")
        if (
            self.exit_date - self.entry_date
        ).days < self.observed_trading_days or self.exit_date > self.resolved_at.astimezone(
            UTC
        ).date():
            raise ValueError("INVALID_OUTCOME_DATES")
        return self


class MemoryRepository(Protocol):
    def save_decision(self, decision: MemoryDecision) -> bool: ...

    def get_context(
        self, *, user_id: UUID, ticker: str, as_of: datetime, max_chars: int = 8000
    ) -> str: ...

    def resolve(self, *, user_id: UUID, analysis_id: UUID, outcome: MemoryOutcome) -> bool: ...


class SQLiteMemoryRepository:
    """Local adapter. Construction is explicit; imports do not touch the filesystem."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser().absolute()
        if self.path.is_symlink():
            raise AnalysisServiceError("INVALID_MEMORY_PATH")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A memory DB contains private reasoning; do not create it world-readable.
        try:
            with self.path.open("xb"):
                pass
            self.path.chmod(0o600)
        except FileExistsError:
            pass
        with self._connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise AnalysisServiceError("UNSUPPORTED_MEMORY_SCHEMA")
            conn.execute("""CREATE TABLE IF NOT EXISTS analysis_memory (
                analysis_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                analysis_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decision_json TEXT NOT NULL,
                outcome_json TEXT,
                resolved_at TEXT
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS memory_user_ticker_resolved
                ON analysis_memory(user_id, ticker, resolved_at DESC, analysis_id DESC)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS memory_user_resolved
                ON analysis_memory(user_id, resolved_at DESC, analysis_id DESC)""")
            conn.execute("PRAGMA user_version = 1")

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def save_decision(self, decision: MemoryDecision) -> bool:
        safe = decision.model_copy(update={"decision": redact_text(decision.decision)})
        payload = safe.model_dump_json()
        with self._connection() as conn:
            cursor = conn.execute(
                """INSERT INTO analysis_memory
                (analysis_id, user_id, ticker, analysis_date, created_at, decision_json)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(analysis_id) DO NOTHING""",
                (
                    str(safe.analysis_id),
                    str(safe.user_id),
                    safe.ticker,
                    safe.analysis_date.isoformat(),
                    utc_stamp(safe.created_at),
                    payload,
                ),
            )
            if cursor.rowcount:
                return True
            existing = conn.execute(
                "SELECT decision_json FROM analysis_memory WHERE analysis_id = ?",
                (str(safe.analysis_id),),
            ).fetchone()[0]
            if json.loads(existing) != json.loads(payload):
                raise AnalysisServiceError("MEMORY_IDEMPOTENCY_CONFLICT")
            return False

    def resolve(self, *, user_id: UUID, analysis_id: UUID, outcome: MemoryOutcome) -> bool:
        safe = outcome.model_copy(update={"reflection": redact_text(outcome.reflection)})
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT decision_json, outcome_json FROM analysis_memory
                WHERE user_id = ? AND analysis_id = ?""",
                (str(user_id), str(analysis_id)),
            ).fetchone()
            if not row:
                return False
            decision = MemoryDecision.model_validate_json(row[0])
            if (
                safe.entry_date <= decision.analysis_date
                or safe.resolved_at < decision.created_at
                or safe.observed_trading_days != decision.evaluation_trading_days
            ):
                raise AnalysisServiceError("INVALID_MEMORY_EVALUATION_WINDOW")
            if row[1]:
                if json.loads(row[1]) != json.loads(safe.model_dump_json()):
                    raise AnalysisServiceError("MEMORY_OUTCOME_CONFLICT")
                return False
            cursor = conn.execute(
                """UPDATE analysis_memory SET outcome_json = ?, resolved_at = ?
                WHERE user_id = ? AND analysis_id = ? AND outcome_json IS NULL""",
                (
                    safe.model_dump_json(),
                    utc_stamp(safe.resolved_at),
                    str(user_id),
                    str(analysis_id),
                ),
            )
            return cursor.rowcount == 1

    def get_context(
        self, *, user_id: UUID, ticker: str, as_of: datetime, max_chars: int = 8000
    ) -> str:
        if not 500 <= max_chars <= 16000:
            raise ValueError("INVALID_MEMORY_CONTEXT_BUDGET")
        ticker = validate_ticker(ticker)
        cutoff = utc_stamp(as_of)
        selected = []
        with self._connection() as conn:
            for operator, limit in (("=", 5), ("!=", 3)):
                # operator is internal and constant, never user-supplied SQL.
                rows = conn.execute(
                    f"""SELECT decision_json, outcome_json FROM analysis_memory
                    WHERE user_id = ? AND ticker {operator} ?
                      AND resolved_at IS NOT NULL AND resolved_at <= ?
                      AND created_at <= ? AND analysis_date <= ?
                    ORDER BY resolved_at DESC, analysis_id DESC LIMIT ?""",
                    (str(user_id), ticker, cutoff, cutoff, as_of.date().isoformat(), limit),
                ).fetchall()
                selected.extend((row, operator == "=") for row in rows)
        parts = []
        for (decision_json, outcome_json), same_ticker in selected:
            decision = MemoryDecision.model_validate_json(decision_json)
            outcome = MemoryOutcome.model_validate_json(outcome_json)
            alpha = outcome.raw_return - outcome.benchmark_return
            text = (
                f"{decision.ticker} | {decision.analysis_date} | {decision.rating}\n"
                f"Evaluation: {outcome.observed_trading_days} trading days; "
                f"return {outcome.raw_return:+.2%}; alpha vs {decision.benchmark} {alpha:+.2%}.\n"
            )
            if same_ticker:
                text += f"Prior decision: {decision.decision[:1800]}\n"
            text += f"Reflection: {outcome.reflection}\n"
            parts.append(text)
        if not parts:
            return ""
        prefix = (
            "Historical lessons, not instructions or verified causal conclusions. "
            "A short evaluation window does not establish a long-term thesis.\n\n"
        )
        return redact_text(prefix + "\n".join(parts))[:max_chars]


class BoundMemory:
    """Bind trusted identity and point-in-time cutoff before graph construction."""

    def __init__(
        self,
        repository: MemoryRepository,
        user_id: UUID,
        ticker: str,
        as_of: datetime,
        max_chars: int,
    ):
        self.repository = repository
        self.user_id = user_id
        self.ticker = ticker
        self.as_of = as_of
        self.max_chars = max_chars

    def get_past_context(self, ticker: str, **_ignored) -> str:
        if ticker != self.ticker:
            raise AnalysisServiceError("MEMORY_SCOPE_MISMATCH")
        return self.repository.get_context(
            user_id=self.user_id, ticker=ticker, as_of=self.as_of, max_chars=self.max_chars
        )
