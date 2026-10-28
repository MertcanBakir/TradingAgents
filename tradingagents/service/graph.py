"""Small graph adapter: no auto-settlement, report files or CLI changes."""

from __future__ import annotations

from tradingagents.graph.trading_graph import TradingAgentsGraph

from .memory import BoundMemory


class HeadlessTradingGraph(TradingAgentsGraph):
    def __init__(self, *, config: dict, analysts: tuple[str, ...], memory: BoundMemory):
        super().__init__(
            selected_analysts=analysts, debug=False, config=config, update_global_config=False
        )
        self.memory_log = memory
        self.memory_read_failed = False

    def _memory_step(self, state):
        try:
            return {
                "past_context": self.memory_log.get_past_context(state["company_of_interest"]),
                "memory_note": "",
            }
        except Exception:
            self.memory_read_failed = True
            return {"past_context": "", "memory_note": "MEMORY_READ_UNAVAILABLE"}

    def record_decision(self, company_name, trade_date, final_state):
        # AnalysisEngine records only a successfully normalized, complete result.
        # In particular, do not write the upstream ticker/date state file or the
        # CLI's shared markdown memory. No LLM is called by this hook.
        return None
