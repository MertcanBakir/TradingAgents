"""One-shot, non-interactive engine shared by a future API and container worker."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import UUID, uuid4

from .config import build_graph_config
from .errors import AnalysisServiceError, classify_error
from .memory import BoundMemory, MemoryDecision, MemoryRepository
from .models import AnalysisRequest, AnalysisResult, ServiceSettings
from .normalize import normalize_result

logger = logging.getLogger(__name__)


def _graph_factory(**kwargs):
    from .graph import HeadlessTradingGraph

    return HeadlessTradingGraph(**kwargs)


class AnalysisEngine:
    def __init__(self, *, settings: ServiceSettings, memory: MemoryRepository, graph_factory=None):
        self.settings = settings
        self.memory = memory
        self.graph_factory = graph_factory or _graph_factory

    def run(
        self, request: AnalysisRequest, *, user_id: UUID, analysis_id: UUID | None = None
    ) -> AnalysisResult:
        # Identity must come from a trusted caller; the local CLI is not an auth
        # server. A future HTTP adapter must verify JWTs before calling here.
        if not isinstance(user_id, UUID) or (
            analysis_id is not None and not isinstance(analysis_id, UUID)
        ):
            raise AnalysisServiceError("INVALID_TRUSTED_IDENTITY")
        analysis_id = analysis_id or uuid4()
        started_at = datetime.now(UTC)
        started_clock = monotonic()
        cutoff = min(started_at, datetime.combine(request.analysis_date, time.max, UTC))
        bound = BoundMemory(
            self.memory, user_id, request.ticker, cutoff, self.settings.memory_context_chars
        )
        try:
            with TemporaryDirectory(prefix=f"ta-analysis-{analysis_id}-") as directory:
                from pathlib import Path

                config = build_graph_config(request, self.settings, Path(directory))
                graph = self.graph_factory(config=config, analysts=request.analysts, memory=bound)
                state, _signal = graph.propagate(request.ticker, request.analysis_date.isoformat())
                result = normalize_result(
                    state=state,
                    request=request,
                    settings=self.settings,
                    analysis_id=analysis_id,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    duration_seconds=monotonic() - started_clock,
                    data_sources={
                        **config.get("data_vendors", {}),
                        **config.get("tool_vendors", {}),
                    },
                )
                if getattr(graph, "memory_read_failed", False):
                    result = result.model_copy(
                        update={
                            "warning_codes": result.warning_codes + ("MEMORY_READ_UNAVAILABLE",)
                        }
                    )
            if result.quality == "complete":
                decision = MemoryDecision(
                    analysis_id=analysis_id,
                    user_id=user_id,
                    ticker=request.ticker,
                    analysis_date=request.analysis_date,
                    rating=result.final_rating,
                    decision=next(
                        s.content_markdown for s in result.sections if s.code == "final_assessment"
                    ),
                    created_at=result.started_at,
                    evaluation_trading_days=self.settings.evaluation_trading_days,
                    benchmark=self.settings.benchmark,
                )
                try:
                    # False means this exact memory was already persisted.
                    self.memory.save_decision(decision)
                    result = result.model_copy(update={"memory_saved": True})
                except AnalysisServiceError as exc:
                    if exc.code == "MEMORY_IDEMPOTENCY_CONFLICT":
                        raise
                    result = self._memory_warning(result)
                except Exception:
                    result = self._memory_warning(result)
            logger.info(
                "analysis_finished analysis_id=%s quality=%s duration=%.2f",
                analysis_id,
                result.quality,
                result.duration_seconds,
            )
            return result
        except AnalysisServiceError:
            raise
        except Exception as exc:
            logger.error(
                "analysis_failed analysis_id=%s error_type=%s", analysis_id, type(exc).__name__
            )
            raise classify_error(exc) from None

    @staticmethod
    def _memory_warning(result: AnalysisResult) -> AnalysisResult:
        return result.model_copy(
            update={"warning_codes": result.warning_codes + ("MEMORY_SAVE_FAILED",)}
        )
