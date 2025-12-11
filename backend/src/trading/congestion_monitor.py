from time import time
from typing import Literal


class CongestionMonitor:
    """
    Monitor network congestion via recent performance samples.
    Uses avg slot time as a proxy; can be extended with prioritization fees when RPC supports it.
    """

    def __init__(self, rpc_client, check_interval_sec: int = 30):
        self.rpc = rpc_client
        self.last_check = 0.0
        self.check_interval_sec = check_interval_sec
        self.current_level: Literal["low", "normal", "high", "critical"] = "normal"

    async def get_congestion_level(self) -> Literal["low", "normal", "high", "critical"]:
        now = time()
        if now - self.last_check < self.check_interval_sec:
            return self.current_level

        try:
            perf = await self.rpc.get_recent_performance_samples(limit=10)
            samples = getattr(perf, "value", None) or []
            if not samples:
                return self.current_level

            avg_slot_time = (
                sum(s.sample_period_secs / s.num_slots for s in samples if s.num_slots > 0) / len(samples)
            )

            if avg_slot_time < 0.4:
                self.current_level = "low"
            elif avg_slot_time < 0.5:
                self.current_level = "normal"
            elif avg_slot_time < 0.7:
                self.current_level = "high"
            else:
                self.current_level = "critical"

            self.last_check = now
        except Exception:
            self.current_level = "normal"

        return self.current_level

    async def get_recent_priority_fees(self) -> dict:
        """
        Attempt to fetch recent prioritization fees (if RPC supports).
        """
        try:
            fees = await self.rpc.get_recent_prioritization_fees()
            vals = getattr(fees, "value", None) or []
            if not vals:
                return {"p50": 50000, "p75": 100000, "p90": 200000, "max": 500000}
            sorted_fees = sorted(getattr(f, "prioritization_fee", 0) for f in vals)
            n = len(sorted_fees)
            return {
                "p50": sorted_fees[n // 2] if n > 0 else 50000,
                "p75": sorted_fees[int(n * 0.75)] if n > 0 else 100000,
                "p90": sorted_fees[int(n * 0.90)] if n > 0 else 200000,
                "max": sorted_fees[-1] if n > 0 else 500000,
            }
        except Exception:
            return {"p50": 50000, "p75": 100000, "p90": 200000, "max": 500000}

