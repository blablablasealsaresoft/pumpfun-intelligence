"""
Trade metrics collection for observability (Item #5).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from time import time
from typing import Optional, Literal, List, Dict

logger = logging.getLogger(__name__)


@dataclass
class TradeMetrics:
    """Per-trade metrics for observability."""

    trade_id: str
    token_mint: str
    timestamp: float = field(default_factory=time)

    # Cluster context (ties to detection)
    cluster_id: Optional[int] = None
    cluster_type: Optional[str] = None
    cluster_score: int = 0

    # Path taken
    path: Literal["raydium_direct", "jupiter", "dca", "panic_sell"] = "jupiter"

    # Sizing
    requested_amount_sol: float = 0.0
    actual_amount_sol: float = 0.0
    sizing_method: Literal["fixed", "dynamic"] = "fixed"
    sizing_capped_by: Optional[str] = None

    # Pool data
    pool_depth_usd: float = 0.0
    expected_impact_bps: int = 0
    actual_slippage_bps: int = 0

    # Safety
    safety_check_passed: bool = True
    safety_warnings: List[str] = field(default_factory=list)

    # Timing (ms)
    total_latency_ms: float = 0.0
    safety_check_ms: float = 0.0
    sizing_ms: float = 0.0
    sell_sim_ms: float = 0.0
    tx_send_ms: float = 0.0

    # Execution
    attempts: int = 1
    success: bool = False
    signature: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    # Fee tuning
    priority_fee_used: int = 0
    congestion_level: str = "normal"

    # Cache hits
    pool_cache_hit: bool = False
    safety_cache_hit: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


class MetricsCollector:
    """Collect and persist trade metrics."""

    def __init__(self):
        self.metrics_file = os.getenv("METRICS_LOG", "logs/trade_metrics.jsonl")
        self.enable_file = os.getenv("ENABLE_METRICS_FILE", "true").lower() == "true"

        self.recent_trades: List[TradeMetrics] = []
        self.max_recent = 100
        self.pnl_events: List[dict] = []
        self.max_pnl_events = 200
        self.exits_executed = 0
        self.latency_samples_ms: List[float] = []
        self.latency_samples_max = 500
        self.path_sent: Dict[str, int] = {}
        self.path_failed: Dict[str, Dict[str, int]] = {}
        self.path_latency_sum: Dict[str, float] = {}
        self.path_latency_count: Dict[str, int] = {}
        self.fee_state: Dict[str, any] = {"priority_fee": 0, "congestion": "unknown"}
        self.positions: Dict[str, float] = {}
        self.cluster_detected: Dict[str, int] = {}  # key "type|bucket"
        self.cluster_autotrade: Dict[str, int] = {}  # key "result|reason"
        self.cluster_score_last: Dict[str, float] = {}
        self.cluster_liquidity_usd_last: Dict[str, float] = {}
        self.cluster_liquidity_sol_last: Dict[str, float] = {}
        self.cluster_pool_age_minutes_last: Dict[str, float] = {}
        self.cluster_liq_delta_5m_usd_last: Dict[str, float] = {}
        self.cluster_holder_growth_24h_last: Dict[str, float] = {}
        self.cluster_liq_delta_30m_usd_last: Dict[str, float] = {}
        self.cluster_unique_wallets_24h_delta_last: Dict[str, float] = {}

        # Aggregates
        self.total_trades = 0
        self.successful_trades = 0
        self.failed_trades = 0
        self.total_latency_ms = 0.0
        self.realized_pnl_sol = 0.0
        self.realized_pnl_positive_sol = 0.0
        self.realized_pnl_negative_sol = 0.0
        self.pnl_wins = 0
        self.pnl_losses = 0

        # Safety
        self.safety_blocks = 0
        self.safety_warnings = 0

        # Snipes
        self.snipes_attempted = 0
        self.snipes_successful = 0
        self.snipes_latency_ms = 0.0
        self.kol_snipes_attempted = 0
        self.kol_snipes_successful = 0
        self.kol_snipes_latency_ms = 0.0

        # Per-cluster tracking
        self.cluster_trade_counts: dict = {}

    def record(self, metrics: TradeMetrics):
        """Record completed trade."""
        self.total_trades += 1
        self.total_latency_ms += metrics.total_latency_ms

        if metrics.success:
            self.successful_trades += 1
        else:
            self.failed_trades += 1

        # Track by cluster
        if metrics.cluster_id:
            self.cluster_trade_counts[metrics.cluster_id] = (
                self.cluster_trade_counts.get(metrics.cluster_id, 0) + 1
            )

        # Keep recent
        self.recent_trades.append(metrics)
        if len(self.recent_trades) > self.max_recent:
            self.recent_trades.pop(0)

        # Persist
        if self.enable_file:
            self._write(metrics)

        # Log
        logger.info(
            f"[METRICS] {metrics.token_mint[:8]}... | "
            f"{'✓' if metrics.success else '✗'} | "
            f"{metrics.path} | "
            f"{metrics.total_latency_ms:.0f}ms | "
            f"cluster={metrics.cluster_id or 'none'}"
        )

    def _write(self, metrics: TradeMetrics):
        try:
            os.makedirs(os.path.dirname(self.metrics_file), exist_ok=True)
            with open(self.metrics_file, "a", encoding="utf-8") as f:
                f.write(metrics.to_json() + "\n")
        except Exception as e:
            logger.error(f"Failed to write metrics: {e}")

    def get_stats(self) -> dict:
        success_rate = self.successful_trades / self.total_trades if self.total_trades > 0 else 0
        avg_latency = self.total_latency_ms / self.total_trades if self.total_trades > 0 else 0
        lat_p50, lat_p90, lat_p99 = self._latency_percentiles(self.latency_samples_ms)
        pnl_24h = self._pnl_24h()
        exposure_count = len(self.positions)
        exposure_sol = sum(self.positions.values())

        return {
            "total_trades": self.total_trades,
            "successful": self.successful_trades,
            "failed": self.failed_trades,
            "success_rate": success_rate,
            "avg_latency_ms": avg_latency,
            "latency_p50_ms": lat_p50,
            "latency_p90_ms": lat_p90,
            "latency_p99_ms": lat_p99,
            "clusters_traded": len(self.cluster_trade_counts),
            "realized_pnl_sol": self.realized_pnl_sol,
            "realized_pnl_positive_sol": self.realized_pnl_positive_sol,
            "realized_pnl_negative_sol": self.realized_pnl_negative_sol,
            "pnl_wins": self.pnl_wins,
            "pnl_losses": self.pnl_losses,
            "realized_pnl_sol_24h": pnl_24h,
            "exits_executed": self.exits_executed,
            "safety_blocks": self.safety_blocks,
            "safety_warnings": self.safety_warnings,
            "snipes_attempted": self.snipes_attempted,
            "snipes_successful": self.snipes_successful,
            "snipes_latency_ms": self.snipes_latency_ms,
            "kol_snipes_attempted": self.kol_snipes_attempted,
            "kol_snipes_successful": self.kol_snipes_successful,
            "kol_snipes_latency_ms": self.kol_snipes_latency_ms,
            "path_sent": self.path_sent,
            "path_failed": self.path_failed,
            "path_latency_sum": self.path_latency_sum,
            "path_latency_count": self.path_latency_count,
            "fee_state": self.fee_state,
            "open_positions_count": exposure_count,
            "open_positions_sol_total": exposure_sol,
            "cluster_detected": self.cluster_detected,
            "cluster_autotrade": self.cluster_autotrade,
            "cluster_score_last": self.cluster_score_last,
            "cluster_liquidity_usd_last": self.cluster_liquidity_usd_last,
            "cluster_liquidity_sol_last": self.cluster_liquidity_sol_last,
            "cluster_pool_age_minutes_last": self.cluster_pool_age_minutes_last,
            "cluster_liq_delta_5m_usd_last": self.cluster_liq_delta_5m_usd_last,
            "cluster_holder_growth_24h_last": self.cluster_holder_growth_24h_last,
            "cluster_liq_delta_30m_usd_last": self.cluster_liq_delta_30m_usd_last,
            "cluster_unique_wallets_24h_delta_last": self.cluster_unique_wallets_24h_delta_last,
        }

    def get_cluster_stats(self, cluster_id: int) -> dict:
        cluster_trades = [t for t in self.recent_trades if t.cluster_id == cluster_id]
        successful = sum(1 for t in cluster_trades if t.success)

        return {
            "cluster_id": cluster_id,
            "total_trades": len(cluster_trades),
            "successful": successful,
            "success_rate": successful / len(cluster_trades) if cluster_trades else 0,
        }

    def record_pnl(
        self,
        token: str,
        symbol: str,
        in_amount_sol: float,
        out_amount_sol: float,
        pnl_sol: float,
        entry_price_usd: Optional[float] = None,
        exit_price_usd: Optional[float] = None,
    ):
        """Record realized PnL event."""
        evt = {
            "token": token,
            "symbol": symbol,
            "in_amount_sol": in_amount_sol,
            "out_amount_sol": out_amount_sol,
            "pnl_sol": pnl_sol,
            "entry_price_usd": entry_price_usd,
            "exit_price_usd": exit_price_usd,
            "ts": time(),
        }
        self.realized_pnl_sol += pnl_sol
        if pnl_sol >= 0:
            self.pnl_wins += 1
            self.realized_pnl_positive_sol += pnl_sol
        else:
            self.pnl_losses += 1
            self.realized_pnl_negative_sol += pnl_sol
        self.exits_executed += 1
        self.pnl_events.append(evt)
        if len(self.pnl_events) > self.max_pnl_events:
            self.pnl_events.pop(0)

    def record_safety_block(self, warnings_count: int):
        self.safety_blocks += 1
        self.safety_warnings += max(0, warnings_count)

    def record_snipe(self, success: bool, latency_ms: float):
        self.snipes_attempted += 1
        if success:
            self.snipes_successful += 1
        self.snipes_latency_ms += max(0.0, latency_ms)

    def record_kol_snipe(self, success: bool, latency_ms: float):
        self.kol_snipes_attempted += 1
        if success:
            self.kol_snipes_successful += 1
        self.kol_snipes_latency_ms += max(0.0, latency_ms)

    def record_trade(self, path: str, success: bool, latency_ms: float, reason: Optional[str]):
        """Record per-path send/fail and latency samples."""
        self.path_sent[path] = self.path_sent.get(path, 0) + 1
        if not success:
            if path not in self.path_failed:
                self.path_failed[path] = {}
            r = reason or "unknown"
            self.path_failed[path][r] = self.path_failed[path].get(r, 0) + 1
        self.path_latency_sum[path] = self.path_latency_sum.get(path, 0.0) + max(0.0, latency_ms)
        self.path_latency_count[path] = self.path_latency_count.get(path, 0) + 1
        if latency_ms >= 0:
            self.latency_samples_ms.append(latency_ms)
            if len(self.latency_samples_ms) > self.latency_samples_max:
                self.latency_samples_ms = self.latency_samples_ms[-self.latency_samples_max :]

    def update_fee_state(self, priority_fee: int, congestion: str):
        self.fee_state = {"priority_fee": priority_fee, "congestion": congestion}

    def position_set(self, token: str, amount_sol: float):
        self.positions[token] = amount_sol

    def position_remove(self, token: str):
        self.positions.pop(token, None)

    def _latency_percentiles(self, samples: List[float]) -> tuple[float, float, float]:
        if not samples:
            return 0.0, 0.0, 0.0
        s = sorted(samples)
        n = len(s)

        def pct(p):
            idx = min(n - 1, int(p * (n - 1)))
            return s[idx]

        return pct(0.5), pct(0.9), pct(0.99)

    def _pnl_24h(self) -> float:
        now = time()
        cutoff = now - 86400
        return sum(evt["pnl_sol"] for evt in self.pnl_events if evt.get("ts", now) >= cutoff)

    def record_cluster_detected(self, cluster_type: str, score: int):
        """
        score is 0-10000; bucket into ranges for observability.
        """
        buckets = [(0, 5000), (5000, 7000), (7000, 8500), (8500, 10001)]
        label = "0-50"
        for low, high in buckets:
            if low <= score < high:
                label = f"{low//100}-{(high-1)//100}"
                break
        key = f"{cluster_type}|{label}"
        self.cluster_detected[key] = self.cluster_detected.get(key, 0) + 1

    def record_cluster_autotrade(self, result: str, reason: str = "none"):
        key = f"{result}|{reason or 'none'}"
        self.cluster_autotrade[key] = self.cluster_autotrade.get(key, 0) + 1

    def update_cluster_last(
        self,
        cluster_type: str,
        score: Optional[float] = None,
        liq_usd: Optional[float] = None,
        liq_sol: Optional[float] = None,
        pool_age_min: Optional[float] = None,
    ):
        if score is not None:
            self.cluster_score_last[cluster_type] = score
        if liq_usd is not None:
            self.cluster_liquidity_usd_last[cluster_type] = liq_usd
        if liq_sol is not None:
            self.cluster_liquidity_sol_last[cluster_type] = liq_sol
        if pool_age_min is not None:
            self.cluster_pool_age_minutes_last[cluster_type] = pool_age_min
        if "liq_delta_5m_usd" in kwargs := {}:
            pass

    def update_cluster_deltas(
        self,
        cluster_type: str,
        liq_delta_5m_usd: Optional[float] = None,
        liq_delta_30m_usd: Optional[float] = None,
        holder_growth_24h: Optional[float] = None,
        unique_wallets_24h_delta: Optional[float] = None,
    ):
        if liq_delta_5m_usd is not None:
            self.cluster_liq_delta_5m_usd_last[cluster_type] = liq_delta_5m_usd
        if liq_delta_30m_usd is not None:
            self.cluster_liq_delta_30m_usd_last[cluster_type] = liq_delta_30m_usd
        if holder_growth_24h is not None:
            self.cluster_holder_growth_24h_last[cluster_type] = holder_growth_24h
        if unique_wallets_24h_delta is not None:
            self.cluster_unique_wallets_24h_delta_last[cluster_type] = unique_wallets_24h_delta


# Global instance
metrics_collector = MetricsCollector()

