"""
Ultra-fast snipe execution for newly detected pools (Item #6).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass
from time import perf_counter
from typing import Optional, List

import requests
from solana.transaction import VersionedTransaction

from geyser_watcher import NewPoolEvent
from trading import TradeMetrics, metrics_collector
from telegram_service import telegram_bot

logger = logging.getLogger(__name__)


@dataclass
class SnipeConfig:
    enabled: bool = True
    max_snipe_sol: float = 0.5
    min_liquidity_sol: float = 1.0
    max_latency_ms: float = 500
    use_jito: bool = True
    jito_tip_lamports: int = 100000
    slippage_bps: int = 1000
    preflight_off: bool = True
    kol_wallets: list = None
    kol_slippage_bps: int = 1200
    kol_priority_mult: float = 3.0
    kol_max_snipe_sol: float = 0.25


class SnipeExecutor:
    """
    Ultra-fast snipe execution for newly detected pools.
    Prioritizes speed over normal safety checks.
    """

    def __init__(self, executor, config: Optional[SnipeConfig] = None):
        self.executor = executor  # Reference to main TradeExecutor
        self.config = config or SnipeConfig(
            enabled=os.getenv("ENABLE_SNIPE", "false").lower() == "true",
            max_snipe_sol=float(os.getenv("MAX_SNIPE_SOL", "0.5")),
            min_liquidity_sol=float(os.getenv("MIN_SNIPE_LIQUIDITY_SOL", "1.0")),
            use_jito=os.getenv("SNIPE_USE_JITO", "true").lower() == "true",
            jito_tip_lamports=int(os.getenv("SNIPE_JITO_TIP_LAMPORTS", "100000")),
            slippage_bps=int(os.getenv("SNIPE_SLIPPAGE_BPS", "1000")),
            preflight_off=os.getenv("SNIPE_PREFLIGHT_OFF", "true").lower() == "true",
            kol_wallets=[w.strip() for w in os.getenv("KOL_WALLETS", "").split(",") if w.strip()],
            kol_slippage_bps=int(os.getenv("KOL_SNIPE_SLIPPAGE_BPS", "1200")),
            kol_priority_mult=float(os.getenv("KOL_SNIPE_PRIORITY_MULT", "3.0")),
            kol_max_snipe_sol=float(os.getenv("KOL_SNIPE_MAX_SOL", "0.25")),
        )

        self._tx_templates = {}
        # Load KOL wallets from file if provided
        kol_file = os.getenv("KOL_WALLETS_FILE", "")
        if kol_file and os.path.exists(kol_file):
            try:
                with open(kol_file, "r", encoding="utf-8") as f:
                    addrs = [line.strip() for line in f if line.strip()]
                    self.config.kol_wallets = addrs
            except Exception:
                pass

        # Stats
        self.snipes_attempted = 0
        self.snipes_successful = 0
        self.total_latency_ms = 0

    async def handle_new_pool(self, event: NewPoolEvent):
        """
        Handle new pool detection - execute snipe if criteria met.
        Called by GeyserWatcher on pool creation.
        """
        if not self.config.enabled:
            return

        start_time = perf_counter()
        is_kol = self._is_kol_event(event)

        logger.info(
            f"[SNIPE] New pool event: {event.pool_type} | token={event.token_mint[:8]}... | KOL={is_kol}"
        )

        if not self._quick_validate(event):
            logger.info("[SNIPE] Validation failed, skipping")
            return

        metrics = TradeMetrics(
            trade_id=f"snipe-{event.signature[:8]}",
            token_mint=event.token_mint,
            path="raydium_direct",
            sizing_method="fixed",
            cluster_type="snipe",
            requested_amount_sol=self.config.max_snipe_sol,
        )

        try:
            self.snipes_attempted += 1

            result = await self._execute_snipe(event, metrics, is_kol=is_kol)

            latency_ms = (perf_counter() - start_time) * 1000
            metrics.total_latency_ms = latency_ms
            self.total_latency_ms += latency_ms
            try:
                if is_kol:
                    metrics_collector.record_kol_snipe(success=bool(result), latency_ms=latency_ms)
                else:
                    metrics_collector.record_snipe(success=bool(result), latency_ms=latency_ms)
            except Exception:
                pass

            if result:
                self.snipes_successful += 1
                metrics.success = True
                metrics.signature = result
                metrics.actual_amount_sol = self.config.max_snipe_sol
                logger.info(f"[SNIPE] ✅ Success: {result} | latency={latency_ms:.0f}ms")
                chat_id = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")
                if chat_id and telegram_bot.enabled:
                    telegram_bot.send_message(
                        chat_id,
                        f"🎯 <b>SNIPE EXECUTED</b>\n\n"
                        f"<b>Type:</b> {event.pool_type}\n"
                        f"<b>Token:</b> <code>{event.token_mint[:12]}...</code>\n"
                        f"<b>Amount:</b> {self.config.max_snipe_sol} SOL\n"
                        f"<b>Latency:</b> {latency_ms:.0f}ms\n"
                        f"<a href='https://solscan.io/tx/{result}'>View TX</a>",
                    )
            else:
                metrics.success = False
                metrics.error_type = "snipe_failed"
                metrics.error_message = "snipe_failed"
                logger.warning(f"[SNIPE] ❌ Failed after {latency_ms:.0f}ms")

            metrics_collector.record(metrics)

        except Exception as e:
            latency_ms = (perf_counter() - start_time) * 1000
            metrics.total_latency_ms = latency_ms
            metrics.success = False
            metrics.error_type = str(type(e).__name__)
            metrics.error_message = str(e)
            metrics_collector.record(metrics)
            logger.error(f"[SNIPE] Error: {e}")

    def _quick_validate(self, event: NewPoolEvent) -> bool:
        """Minimal validation for speed."""
        if event.initial_liquidity_sol > 0 and event.initial_liquidity_sol < self.config.min_liquidity_sol:
            return False
        if not event.token_mint or len(event.token_mint) < 32:
            return False
        if event.quote_mint != "So11111111111111111111111111111111111111112":
            return False
        return True

    async def _execute_snipe(self, event: NewPoolEvent, metrics: TradeMetrics, is_kol: bool = False) -> Optional[str]:
        """Execute the actual snipe with maximum speed."""
        amount_sol = min(
            self.config.max_snipe_sol,
            self.config.kol_max_snipe_sol if is_kol and self.config.kol_max_snipe_sol > 0 else self.config.max_snipe_sol,
        )
        slip = self.config.kol_slippage_bps if is_kol else self.config.slippage_bps
        fee_mult = self.config.kol_priority_mult if is_kol else 2.0

        if self.config.use_jito and getattr(self.executor, "enable_jito", False):
            sig = await self._snipe_via_jito(event, amount_sol, metrics, slip, fee_mult)
            if sig:
                return sig

        return await self._snipe_direct(event, amount_sol, metrics, slip, fee_mult)

    async def _snipe_via_jito(self, event: NewPoolEvent, amount_sol: float, metrics: TradeMetrics, slippage_bps: int, fee_mult: float) -> Optional[str]:
        """Snipe via Jito bundle."""
        try:
            quote = self.executor._get_quote_full(
                token_mint=event.token_mint,
                slippage_bps=slippage_bps,
                amount_sol=amount_sol,
                priority_fee=int(self.executor.priority_fee_microlamports * fee_mult),
            )
            if not quote:
                return None

            payload = {
                "quoteResponse": quote,
                "userPublicKey": self.executor.public_key_str,
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": int(self.executor.priority_fee_microlamports * fee_mult),
                "asLegacyTransaction": False,
            }
            if self.executor.compute_unit_limit > 0:
                payload["computeUnitLimit"] = self.executor.compute_unit_limit

            resp = requests.post("https://quote-api.jup.ag/v6/swap", json=payload, timeout=self.executor.request_timeout)
            if resp.status_code != 200:
                logger.warning(f"[SNIPE] Jito build error: {resp.status_code} {resp.text}")
                return None
            swap_data = resp.json()
            tx_base64 = swap_data.get("swapTransaction")
            if not tx_base64:
                return None
            tx_bytes = base64.b64decode(tx_base64)
            tx = VersionedTransaction.deserialize(tx_bytes)
            tx.sign([self.executor.keypair])
            raw_tx = tx.serialize()
            sig = self.executor._send_via_jito(raw_tx, panic=False, aggressive=True)
            return sig
        except Exception as e:
            logger.error(f"[SNIPE] Jito path error: {e}")
            return None

    async def _snipe_direct(self, event: NewPoolEvent, amount_sol: float, metrics: TradeMetrics, slippage_bps: int, fee_mult: float) -> Optional[str]:
        """Direct snipe without Jito."""
        try:
            # Prefer Raydium direct when applicable
            if event.pool_type in {"raydium_create", "pump_graduation"}:
                try:
                    sig_or_dry = await self.executor.raydium.try_swap(
                        token_mint=event.token_mint,
                        amount_sol=amount_sol,
                        slippage_bps=slippage_bps,
                        priority_fee=int(self.executor._current_priority_fee() * fee_mult),
                    )
                    if sig_or_dry and not isinstance(sig_or_dry, dict):
                        return sig_or_dry
                except Exception as e:
                    logger.debug(f"[SNIPE] Raydium fast path error: {e}")

            # Fallback to Jupiter quote + swap
            quote = self.executor._get_quote_full(
                token_mint=event.token_mint,
                slippage_bps=slippage_bps,
                amount_sol=amount_sol,
                priority_fee=int(self.executor.priority_fee_microlamports * fee_mult),
            )
            if not quote:
                return None

            tx_sig = await asyncio.get_event_loop().run_in_executor(
                None,
                self.executor._execute_swap_with_retry,
                quote,
                False,
                False,
            )
            return tx_sig
        except Exception as e:
            logger.error(f"[SNIPE] Direct error: {e}")
            return None

    def get_stats(self) -> dict:
        success_rate = self.snipes_successful / self.snipes_attempted if self.snipes_attempted > 0 else 0
        avg_latency = self.total_latency_ms / self.snipes_attempted if self.snipes_attempted > 0 else 0

        return {
            "enabled": self.config.enabled,
            "attempted": self.snipes_attempted,
            "successful": self.snipes_successful,
            "success_rate": success_rate,
            "avg_latency_ms": avg_latency,
        }

    def _is_kol_event(self, event: NewPoolEvent) -> bool:
        """Detect if event is likely from a KOL wallet (accountKeys contains allowlisted wallet)."""
        if not self.config.kol_wallets:
            return False
        raw = event.raw_data or {}
        try:
            tx = raw.get("transaction", {}) or raw.get("value", {}).get("transaction", {}) or raw
            msg = tx.get("message", {}) if isinstance(tx, dict) else {}
            keys = msg.get("accountKeys", []) or []
            keys = [k.lower() for k in keys if isinstance(k, str)]
            for w in self.config.kol_wallets:
                if w.lower() in keys:
                    return True
        except Exception:
            return False
        return False


