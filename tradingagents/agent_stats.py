"""Run-scoped agent measurements shared by CLI, reports and service workers.

Only names, counters and timings are retained; prompts, tool arguments and
responses never enter a report. Counts describe logical calls, not SDK retries
or individual HTTP requests hidden inside a vendor library.
"""

import contextlib
from collections import Counter
from functools import wraps
from threading import RLock
from time import monotonic
from typing import Literal

from langchain_core.callbacks import BaseCallbackHandler
from pydantic import BaseModel, ConfigDict, Field

AGENTS = frozenset(
    {
        "Market Analyst",
        "Sentiment Analyst",
        "News Analyst",
        "Fundamentals Analyst",
        "Bull Researcher",
        "Bear Researcher",
        "Research Manager",
        "Trader",
        "Aggressive Analyst",
        "Conservative Analyst",
        "Neutral Analyst",
        "Portfolio Manager",
        "Memory Log",
    }
)


class AgentRunStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    duration_seconds: float = Field(ge=0)
    status: Literal["running", "completed", "error"]
    llm_calls: int = Field(default=0, ge=0)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    usage_missing: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    tools: dict[str, int] = Field(default_factory=dict)
    data_accesses: dict[str, int] = Field(default_factory=dict)


class AgentStats(AgentRunStats):
    agent: str
    runs: tuple[AgentRunStats, ...] = ()


class AgentStatsHandler(BaseCallbackHandler):
    """Associate callbacks by parent run ID, including nested parallel graphs."""

    run_inline = True

    def __init__(self):
        self._lock = RLock()
        self.reset()

    def reset(self):
        with self._lock:
            self._owners = {}
            self._runs = {}
            self._calls = {}

    def on_chain_start(
        self, serialized, inputs, *, run_id, parent_run_id=None, name=None, metadata=None, **kwargs
    ):
        with self._lock:
            owner = self._owners.get(parent_run_id)
            node = (metadata or {}).get("langgraph_node")
            if owner is None and node in AGENTS:
                owner = run_id
                self._runs[owner] = {
                    "agent": node,
                    "started": monotonic(),
                    "ended": None,
                    "status": "running",
                    "llm_calls": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "usage_missing": 0,
                    "errors": 0,
                    "tools": Counter(),
                    "data_accesses": Counter(),
                }
            self._owners[run_id] = owner

    def _end(self, run_id, failed=False):
        with self._lock:
            if run_id in self._runs:
                row = self._runs[run_id]
                row.update(ended=monotonic(), status="error" if failed else "completed")
                row["errors"] = max(row["errors"], int(failed))

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._end(run_id)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._end(run_id, True)

    def _start_call(self, kind, serialized, run_id, parent_run_id):
        with self._lock:
            if run_id in self._calls:
                return
            owner = self._owners.get(parent_run_id)
            self._owners[run_id] = owner
            self._calls[run_id] = (owner, kind)
            if owner is not None:
                row = self._runs[owner]
                if kind == "llm":
                    row["llm_calls"] += 1
                    row["usage_missing"] += 1
                else:
                    row["tools"][(serialized or {}).get("name", "unknown")] += 1

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        self._start_call("llm", serialized, run_id, parent_run_id)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, **kwargs):
        self._start_call("llm", serialized, run_id, parent_run_id)

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        self._start_call("tool", serialized, run_id, parent_run_id)

    def on_llm_end(self, response, *, run_id, **kwargs):
        with self._lock:
            owner, kind = self._calls.pop(run_id, (None, None))
            if owner is None:
                return
            usage = None
            with contextlib.suppress(IndexError, AttributeError, TypeError):
                usage = getattr(response.generations[0][0].message, "usage_metadata", None)
            if usage is None:
                raw = (response.llm_output or {}).get("token_usage") or (
                    response.llm_output or {}
                ).get("usage")
                if raw and "prompt_tokens" in raw and "completion_tokens" in raw:
                    usage = {
                        "input_tokens": raw["prompt_tokens"],
                        "output_tokens": raw["completion_tokens"],
                    }
            if usage is not None and "input_tokens" in usage and "output_tokens" in usage:
                row = self._runs[owner]
                row["tokens_in"] += usage.get("input_tokens", 0)
                row["tokens_out"] += usage.get("output_tokens", 0)
                row["usage_missing"] -= 1

    def _call_error(self, run_id):
        with self._lock:
            owner, _ = self._calls.pop(run_id, (None, None))
            if owner is not None:
                self._runs[owner]["errors"] += 1

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._call_error(run_id)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._call_error(run_id)

    def on_tool_end(self, output, *, run_id, **kwargs):
        # ToolNode can return handled errors as ToolMessages instead of raising.
        if getattr(output, "status", None) == "error":
            self._call_error(run_id)
        else:
            with self._lock:
                self._calls.pop(run_id, None)

    def data_access(self, parent_run_id, name, failed=False):
        with self._lock:
            owner = self._owners.get(parent_run_id)
            if owner is not None:
                self._runs[owner]["data_accesses"][name] += 1
                self._runs[owner]["errors"] += int(failed)

    def snapshot(self):
        with self._lock:
            grouped = {}
            now = monotonic()
            for row in self._runs.values():
                values = {
                    k: row[k]
                    for k in (
                        "status",
                        "llm_calls",
                        "tokens_in",
                        "tokens_out",
                        "usage_missing",
                        "errors",
                    )
                }
                values.update(
                    duration_seconds=max(0, (row["ended"] or now) - row["started"]),
                    tools=dict(row["tools"]),
                    data_accesses=dict(row["data_accesses"]),
                )
                grouped.setdefault(row["agent"], []).append(AgentRunStats(**values))
            result = []
            for agent, runs in grouped.items():
                totals = {
                    k: sum(getattr(r, k) for r in runs)
                    for k in (
                        "duration_seconds",
                        "llm_calls",
                        "tokens_in",
                        "tokens_out",
                        "usage_missing",
                        "errors",
                    )
                }
                for key in ("tools", "data_accesses"):
                    counter = Counter()
                    for r in runs:
                        counter.update(getattr(r, key))
                    totals[key] = dict(counter)
                status = (
                    "error"
                    if any(r.status == "error" for r in runs)
                    else "running"
                    if any(r.status == "running" for r in runs)
                    else "completed"
                )
                result.append(
                    AgentStats(agent=agent, status=status, runs=tuple(runs), **totals).model_dump(
                        mode="json"
                    )
                )
            return result


def track_data(name):
    """Count logical data fetches, including ones outside LangChain tools."""

    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                from langgraph.config import get_config

                callbacks = get_config().get("callbacks")
                handlers = getattr(callbacks, "handlers", ())
                parent = getattr(callbacks, "parent_run_id", None)
            except RuntimeError:
                handlers, parent = (), None
            failed = False
            try:
                return fn(*args, **kwargs)
            except Exception:
                failed = True
                raise
            finally:
                for handler in handlers:
                    if isinstance(handler, AgentStatsHandler):
                        handler.data_access(
                            parent,
                            str(args[0]) if name == "vendor_router" and args else name,
                            failed,
                        )

        return wrapped

    return decorate


def render_agent_stats(rows, language="en"):
    tr = language == "tr"
    headers = (
        "Agent | Süre | Model çağrısı | Giriş tokenı | Çıkış tokenı | Tool çağrısı | Durum"
        if tr
        else "Agent | Duration | Model calls | Input tokens | Output tokens | Tool calls | Status"
    )
    lines = ["| " + headers + " |", "|---|---:|---:|---:|---:|---:|---|"]
    for row in rows:
        missing = row["usage_missing"]
        suffix = " + ?" if missing else ""
        lines.append(
            f"| {row['agent']} | {row['duration_seconds']:.2f}s | {row['llm_calls']} | {row['tokens_in']:,}{suffix} | {row['tokens_out']:,}{suffix} | {sum(row['tools'].values())} | {row['status']} |"
        )
    if not rows:
        lines.append("\nÖlçüm bulunmuyor." if tr else "\nNo measurements available.")
    lines.append(
        "\nSüreler paralel çalışmaları içerir; toplam analiz süresi için toplanmaz. '?' eksik token bilgisidir. Çağrılar mantıksal çağrılardır; SDK tekrarları/HTTP istekleri değildir."
        if tr
        else "\nDurations overlap during parallel work; do not sum them as analysis elapsed time. '?' means missing token usage. Counts are logical calls, not SDK retries or HTTP requests."
    )
    for row in rows:
        lines.append(f"\n##### {row['agent']}")
        for label, key in (
            ("Tools", "tools"),
            ("Veri erişimleri" if tr else "Data accesses", "data_accesses"),
        ):
            lines.append(
                f"- {label}: "
                + (", ".join(f"{k} ×{v}" for k, v in sorted(row[key].items())) or "—")
            )
        lines.append(f"- {'Hatalar' if tr else 'Errors'}: {row['errors']}")
        if len(row.get("runs", ())) > 1:
            for i, run in enumerate(row["runs"], 1):
                lines.append(
                    f"- {'Çalıştırma' if tr else 'Run'} {i}: {run['duration_seconds']:.2f}s; LLM {run['llm_calls']}; tokens {run['tokens_in']}/{run['tokens_out']}; missing {run['usage_missing']}; tools {sum(run['tools'].values())}; {run['status']}"
                )
    return "\n".join(lines)


def append_agent_stats(decision, rows, language="en"):
    """Render measurements inside the final decision while preserving the raw decision."""
    if not rows:
        return decision
    title = "Agent Çalışma İstatistikleri" if language == "tr" else "Agent Execution Statistics"
    return f"{decision.rstrip()}\n\n#### {title}\n\n{render_agent_stats(rows, language)}"
