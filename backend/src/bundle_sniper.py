"""
Bundle sniper: uses KOLSniper fast path to snipe detected launches.
"""

from __future__ import annotations

import asyncio
import os
import logging
from time import perf_counter
from typing import Optional

from bundle_detector import BundleLaunchEvent
from kol_sniper import KOLSniper
from trading import metrics_collector

logger = logging.getLogger(__name__)


class BundleSniper:
    def __init__(self, kol_sniper: KOLSniper):
        self.sniper = kol_sniper
        self.enabled = os.getenv("ENABLE_BUNDLE_SNIPE", "false").lower() in {"1", "true", "yes", "on"}
        self.max_amount_sol = float(os.getenv("BUNDLE_SNIPE_MAX_SOL", "0.5"))
        self.min_confidence = float(os.getenv("BUNDLE_SNIPE_MIN_CONFIDENCE", "0.7"))
        self.min_volume_sol = float(os.getenv("BUNDLE_MIN_VOLUME_SOL", "2.0"))
        self.skip_dex = set([d.strip() for d in os.getenv("BUNDLE_SKIP_DEX", "").split(",") if d.strip()])
        self.amounts = {
            "new_pool": float(os.getenv("SNIPE_NEW_POOL_SOL", "0.2")),
            "pump_graduation": float(os.getenv("SNIPE_PUMP_GRAD_SOL", "0.3")),
            "coordinated_buy": float(os.getenv("SNIPE_COORD_BUY_SOL", "0.1")),
            "whale_entry": float(os.getenv("SNIPE_WHALE_SOL", "0.15")),
        }
        self.snipes_attempted = 0
        self.snipes_successful = 0

    async def handle_launch(self, event: BundleLaunchEvent):
        if not self.enabled:
            return
        if not self._should_snipe(event):
            return
        start = perf_counter()
        amt = min(self.amounts.get(event.event_type, 0.1), self.max_amount_sol)
        amt *= event.confidence
        amt = max(0.01, amt)
        self.snipes_attempted += 1
        try:
            sig = await self.sniper._execute_snipe_for_token(event.token_mint, amt, reason=f"bundle_{event.event_type}")
            latency_ms = (perf_counter() - start) * 1000
            if sig:
                self.snipes_successful += 1
                try:
                    metrics_collector.record_snipe(success=True, latency_ms=latency_ms)
                except Exception:
                    pass
                # Track position via PositionManager if available
                try:
                    self.sniper._record_position(
                        token_mint=event.token_mint,
                        amount_sol=amt,
                        sig=sig,
                        source="bundle",
                        source_details={
                            "event_type": event.event_type,
                            "confidence": event.confidence,
                            "dex": event.dex,
                            "slot": event.slot,
                        },
                    )
                except Exception:
                    pass
                logger.info(
                    f"[BUNDLE-SNIPE] ✅ {event.token_mint[:8]}... {amt:.4f} SOL | {event.event_type} | {latency_ms:.0f}ms"
                )
            else:
                logger.warning(f"[BUNDLE-SNIPE] ❌ Failed {event.token_mint[:8]}... {event.event_type}")
        except Exception as e:
            logger.error(f"[BUNDLE-SNIPE] Error: {e}")

    def _should_snipe(self, event: BundleLaunchEvent) -> bool:
        if event.confidence < self.min_confidence:
            return False
        if event.dex in self.skip_dex:
            return False
        if event.event_type == "coordinated_buy" and event.total_sol_volume < self.min_volume_sol:
            return False
        return True

    def get_stats(self) -> dict:
        rate = self.snipes_successful / self.snipes_attempted if self.snipes_attempted else 0
        return {"enabled": self.enabled, "attempted": self.snipes_attempted, "successful": self.snipes_successful, "success_rate": rate}

