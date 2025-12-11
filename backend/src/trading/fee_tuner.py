from collections import deque
from dataclasses import dataclass, field
from time import time
from typing import Literal, Optional
import os


@dataclass
class FeeConfig:
    base_fee_microlamports: int = 50_000
    min_fee_microlamports: int = 10_000
    max_fee_microlamports: int = 1_000_000
    success_decrease_pct: float = 10.0
    failure_increase_pct: float = 50.0
    timeout_increase_pct: float = 25.0
    high_congestion_multiplier: float = 2.0
    critical_congestion_multiplier: float = 4.0
    adjustment_cooldown_sec: int = 30
    reset_after_sec: int = 300


@dataclass
class FeeState:
    current_fee: int
    last_adjustment: float = 0.0
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    recent_outcomes: deque = field(default_factory=lambda: deque(maxlen=20))
    congestion_level: Literal["low", "normal", "high", "critical"] = "normal"


class PriorityFeeTuner:
    def __init__(self, config: Optional[FeeConfig] = None):
        self.config = config or FeeConfig(
            base_fee_microlamports=int(os.getenv("PRIORITY_FEE_MICROLAMPORTS", "50000")),
            min_fee_microlamports=int(os.getenv("MIN_PRIORITY_FEE_MICROLAMPORTS", "10000")),
            max_fee_microlamports=int(os.getenv("MAX_PRIORITY_FEE_MICROLAMPORTS", "1000000")),
            success_decrease_pct=float(os.getenv("FEE_SUCCESS_DECREASE_PCT", "10")),
            failure_increase_pct=float(os.getenv("FEE_FAILURE_INCREASE_PCT", "50")),
            timeout_increase_pct=float(os.getenv("FEE_TIMEOUT_INCREASE_PCT", "25")),
            high_congestion_multiplier=float(os.getenv("FEE_HIGH_CONGESTION_MULTIPLIER", "2.0")),
            critical_congestion_multiplier=float(os.getenv("FEE_CRITICAL_CONGESTION_MULTIPLIER", "4.0")),
            adjustment_cooldown_sec=int(os.getenv("FEE_ADJUSTMENT_COOLDOWN_SEC", "30")),
            reset_after_sec=int(os.getenv("FEE_RESET_AFTER_SEC", "300")),
        )
        self.state = FeeState(current_fee=self.config.base_fee_microlamports)

    def get_current_fee(self) -> int:
        multiplier = {
            "low": 0.75,
            "normal": 1.0,
            "high": self.config.high_congestion_multiplier,
            "critical": self.config.critical_congestion_multiplier,
        }.get(self.state.congestion_level, 1.0)
        adjusted = int(self.state.current_fee * multiplier)
        return self._clamp_fee(adjusted)

    def record_outcome(
        self,
        success: bool,
        latency_ms: Optional[float] = None,
        error_type: Optional[str] = None,
    ) -> int:
        now = time()
        if now - self.state.last_adjustment < self.config.adjustment_cooldown_sec:
            return self.state.current_fee

        self.state.recent_outcomes.append(
            {
                "success": success,
                "latency_ms": latency_ms,
                "error_type": error_type,
                "timestamp": now,
            }
        )

        if success:
            self.state.consecutive_successes += 1
            self.state.consecutive_failures = 0
            if self.state.consecutive_successes >= 3:
                decrease = 1 - (self.config.success_decrease_pct / 100)
                self.state.current_fee = int(self.state.current_fee * decrease)
                self.state.last_adjustment = now
        else:
            self.state.consecutive_failures += 1
            self.state.consecutive_successes = 0
            if error_type in ("timeout", "blockhash_expired"):
                increase = 1 + (self.config.timeout_increase_pct / 100)
            else:
                increase = 1 + (self.config.failure_increase_pct / 100)
            self.state.current_fee = int(self.state.current_fee * increase)
            self.state.last_adjustment = now

        self.state.current_fee = self._clamp_fee(self.state.current_fee)
        return self.state.current_fee

    def update_congestion(self, level: Literal["low", "normal", "high", "critical"]):
        self.state.congestion_level = level

    def reset_to_base(self):
        self.state.current_fee = self.config.base_fee_microlamports
        self.state.consecutive_successes = 0
        self.state.consecutive_failures = 0
        self.state.last_adjustment = 0.0

    def _clamp_fee(self, fee: int) -> int:
        return max(self.config.min_fee_microlamports, min(fee, self.config.max_fee_microlamports))

    def get_stats(self) -> dict:
        recent_success_rate = (
            sum(1 for o in self.state.recent_outcomes if o["success"]) / len(self.state.recent_outcomes)
            if self.state.recent_outcomes
            else 0
        )
        return {
            "current_fee": self.state.current_fee,
            "effective_fee": self.get_current_fee(),
            "base_fee": self.config.base_fee_microlamports,
            "congestion_level": self.state.congestion_level,
            "consecutive_successes": self.state.consecutive_successes,
            "consecutive_failures": self.state.consecutive_failures,
            "recent_success_rate": recent_success_rate,
            "recent_trades": len(self.state.recent_outcomes),
        }

