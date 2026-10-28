"""Scheduled, price-evidence-based memory settlement; no extra summarizing LLM.

US stock scope only. First complete session *after* the decision's actual
creation date, then five session-to-session close intervals. Adjusted closes
for both stock and SPY; missing/invalid evidence defers instead of fabricating.
Lessons are deterministic observations, not causal claims or model training.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from tradingagents.dataflows.evaluation_prices import AdjustedCloseSource

from ..errors import AnalysisServiceError
from ..memory import MemoryDecision, MemoryOutcome
from .repository import SupabaseMemoryRepository


def evaluation_sessions(decision, now):
    import pandas_market_calendars as mcal

    start = max(
        decision.analysis_date, decision.created_at.astimezone(ZoneInfo("America/New_York")).date()
    ) + timedelta(days=1)
    if start > now.date():
        return []
    # Bound each evaluation to <= one year even for an old stalled row.
    schedule = mcal.get_calendar("NASDAQ").schedule(
        start_date=start, end_date=min(now.date(), start + timedelta(days=365))
    )
    schedule = schedule[schedule["market_close"] <= now - timedelta(hours=1)]
    needed = decision.evaluation_trading_days + 1
    if len(schedule) < needed:
        return []
    return [stamp.date() for stamp in schedule.index[:needed]]


def make_outcome(decision: MemoryDecision, sessions, stock, benchmark, now):
    if len(sessions) != decision.evaluation_trading_days + 1:
        raise AnalysisServiceError("EVALUATION_NOT_DUE")
    # Require every session in both series; do not silently skip missing days.
    for series in (stock, benchmark):
        if any(
            day not in series or not math.isfinite(series[day]) or series[day] <= 0
            for day in sessions
        ):
            raise AnalysisServiceError("EVALUATION_PRICES_MISSING")
    start, end = sessions[0], sessions[-1]
    raw = stock[end] / stock[start] - 1
    bench = benchmark[end] / benchmark[start] - 1
    orientation = {
        "Buy": "bullish",
        "Overweight": "bullish",
        "Sell": "bearish",
        "Underweight": "bearish",
        "Hold": "neutral",
    }[decision.rating]
    reflection = (
        f"Observed {decision.evaluation_trading_days} trading-session intervals after the decision; "
        f"adjusted-close return {raw:+.2%}, {decision.benchmark} {bench:+.2%}, relative return {raw - bench:+.2%}. "
        f"The prior view was {orientation}. This is a short-window observation, not proof of a causal explanation "
        "or of the long-term thesis. Recheck fresh evidence rather than copying the previous recommendation."
    )
    return MemoryOutcome(
        raw_return=raw,
        benchmark_return=bench,
        entry_date=start,
        exit_date=end,
        observed_trading_days=decision.evaluation_trading_days,
        reflection=reflection,
        resolved_at=now,
    )


class EvaluationJob:
    def __init__(self, repository, *, prices=None, calendar=evaluation_sessions, now=None):
        self.repository = repository
        self.prices = prices or AdjustedCloseSource()
        self.calendar = calendar
        self.now = now or (lambda: datetime.now(UTC))

    def run(self, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError("INVALID_EVALUATION_LIMIT")
        counts = {"evaluated": 0, "deferred": 0, "failed": 0}
        memory = SupabaseMemoryRepository(self.repository)
        # Claim one row at a time: a batch never holds leases it cannot service.
        for _ in range(limit):
            token = uuid4()
            with self.repository.transaction() as conn:
                row = conn.execute(
                    """WITH candidate AS (
                    SELECT m.analysis_id FROM analysis_backend.memory m
                    JOIN analysis_backend.jobs j ON j.id=m.analysis_id AND j.user_id=m.user_id
                    WHERE m.outcome IS NULL AND j.status='success' AND m.next_evaluation_at<=clock_timestamp()
                    AND (%s::uuid IS NULL OR m.user_id=%s::uuid)
                    AND (m.evaluation_until IS NULL OR m.evaluation_until<=clock_timestamp())
                    ORDER BY m.next_evaluation_at,m.analysis_id FOR UPDATE OF m SKIP LOCKED LIMIT 1)
                    UPDATE analysis_backend.memory m SET evaluation_token=%s,
                    evaluation_until=clock_timestamp()+interval '5 minutes',evaluation_attempts=evaluation_attempts+1
                    FROM candidate c WHERE m.analysis_id=c.analysis_id RETURNING m.*""",
                    (
                        self.repository.settings.personal_user_id,
                        self.repository.settings.personal_user_id,
                        token,
                    ),
                ).fetchone()
            if not row:
                break
            try:
                d = MemoryDecision.model_validate(row["decision"])
                now = self.now()
                sessions = self.calendar(d, now)
                if not sessions:
                    raise AnalysisServiceError("EVALUATION_NOT_DUE")
                stock = self.prices.closes(d.ticker, sessions[0], sessions[-1])
                benchmark = self.prices.closes(d.benchmark, sessions[0], sessions[-1])
                outcome = make_outcome(d, sessions, stock, benchmark, now)
                memory.resolve(
                    user_id=d.user_id,
                    analysis_id=d.analysis_id,
                    outcome=outcome,
                    evaluation_token=token,
                )
                counts["evaluated"] += 1
            except Exception as exc:
                code = (
                    exc.code
                    if isinstance(exc, AnalysisServiceError)
                    else "EVALUATION_SOURCE_UNAVAILABLE"
                )
                counts[
                    "deferred"
                    if code in ("EVALUATION_NOT_DUE", "EVALUATION_PRICES_MISSING")
                    else "failed"
                ] += 1
                with self.repository.transaction() as conn:
                    conn.execute(
                        """UPDATE analysis_backend.memory SET evaluation_token=NULL,evaluation_until=NULL,
                        next_evaluation_at=clock_timestamp()+interval '12 hours',last_evaluation_code=%s
                        WHERE user_id=%s AND analysis_id=%s AND evaluation_token=%s AND outcome IS NULL""",
                        (code, row["user_id"], row["analysis_id"], token),
                    )
        return counts
