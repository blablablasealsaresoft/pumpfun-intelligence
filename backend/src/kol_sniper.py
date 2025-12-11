"""
KOL sniper: copies KOL buys with preflight-off Jupiter swap, Jito-first submission.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass
from time import perf_counter
from typing import Optional

import requests
from solders.transaction import VersionedTransaction

from kol_watcher import KOLBuyEvent
from dexscreener_api import dexscreener
from trading import TradeMetrics, metrics_collector
from telegram_service import telegram_bot

logger = logging.getLogger(__name__)


@dataclass
class KOLSnipeConfig:
    enabled: bool = True
    fixed_amount_sol: float = 0.1
    max_amount_sol: float = 1.0
    min_amount_sol: float = 0.02
    slippage_bps: int = 1500
    priority_fee_multiplier: float = 3.0
    use_jito: bool = True
    jito_tip_lamports: int = 200_000
    min_kol_buy_sol: float = 0.3
    max_kol_buy_sol: float = 200.0
    max_blockhash_age_slots: int = 20


class KOLSniper:
    def __init__(self, executor, config: Optional[KOLSnipeConfig] = None):
        self.executor = executor
        self.config = config or KOLSnipeConfig(
            enabled=os.getenv("ENABLE_KOL_SNIPE", "false").lower() == "true",
            fixed_amount_sol=float(os.getenv("KOL_SNIPE_AMOUNT_SOL", "0.1")),
            max_amount_sol=float(os.getenv("KOL_SNIPE_MAX_SOL", "1.0")),
            min_amount_sol=float(os.getenv("KOL_SNIPE_MIN_SOL", "0.02")),
            slippage_bps=int(os.getenv("KOL_SNIPE_SLIPPAGE_BPS", "1500")),
            priority_fee_multiplier=float(os.getenv("KOL_SNIPE_PRIORITY_MULT", "3.0")),
            use_jito=os.getenv("KOL_SNIPE_USE_JITO", "true").lower() in {"1", "true", "yes", "on"},
            jito_tip_lamports=int(os.getenv("KOL_SNIPE_JITO_TIP", "200000")),
            min_kol_buy_sol=float(os.getenv("KOL_MIN_BUY_SOL", "0.3")),
            max_kol_buy_sol=float(os.getenv("KOL_MAX_BUY_SOL", "200")),
            max_blockhash_age_slots=int(os.getenv("KOL_MAX_BLOCKHASH_AGE_SLOTS", "20")),
        )

    async def handle_kol_buy(self, event: KOLBuyEvent):
        if not self.config.enabled:
            return
        if event.amount_sol < self.config.min_kol_buy_sol or event.amount_sol > self.config.max_kol_buy_sol:
            return

        start = perf_counter()
        amount_sol = self._calc_amount(event.amount_sol)

        metrics = TradeMetrics(
            trade_id=f"kol-{event.signature[:8]}",
            token_mint=event.token_mint,
            path="kol",
            sizing_method="fixed",
            cluster_type="kol_snipe",
            requested_amount_sol=amount_sol,
        )

        try:
            # Get quote with high priority fee
            quote = self.executor._get_quote_full(
                token_mint=event.token_mint,
                slippage_bps=self.config.slippage_bps,
                amount_sol=amount_sol,
                priority_fee=int(self.executor.priority_fee_microlamports * self.config.priority_fee_multiplier),
            )
            if not quote:
                return

            sig = await self._send_fast(quote)
            latency_ms = (perf_counter() - start) * 1000
            metrics.total_latency_ms = latency_ms

            if sig:
                metrics.success = True
                metrics.signature = sig
                metrics.actual_amount_sol = amount_sol
                metrics_collector.record_kol_snipe(success=True, latency_ms=latency_ms)
                self._record_position(event.token_mint, amount_sol, sig, source="kol", source_details={"kol": event.kol_name, "dex": event.dex, "kol_sig": event.signature})
                self._alert(event, amount_sol, sig, latency_ms, True)
            else:
                metrics.success = False
                metrics.error_type = "kol_snipe_failed"
                metrics_collector.record_kol_snipe(success=False, latency_ms=latency_ms)
                self._alert(event, amount_sol, None, latency_ms, False)

            metrics_collector.record(metrics)
        except Exception as e:
            metrics.success = False
            metrics.error_type = str(type(e).__name__)
            metrics.error_message = str(e)
            metrics.total_latency_ms = (perf_counter() - start) * 1000
            metrics_collector.record(metrics)
            logger.error(f"[KOL-SNIPE] Error: {e}")

    async def _send_fast(self, quote) -> Optional[str]:
        """Jito-first submission; fallback to RPC."""
        try:
            swap_data = self._build_swap_tx(quote)
            if not swap_data:
                return None
            tx_bytes = swap_data
            tx = VersionedTransaction.deserialize(tx_bytes)
            tx.sign([self.executor.keypair])
            raw_tx = tx.serialize()

            # Jito first
            if self.config.use_jito:
                sig = self.executor._send_via_jito(raw_tx, panic=False, aggressive=True)
                if sig:
                    return sig
            # Fallback RPC
            for client in self.executor.rpc_clients:
                try:
                    resp_send = client.send_raw_transaction(raw_tx, skip_preflight=True)
                    tx_sig = resp_send.get("result") or resp_send.get("value") or resp_send
                    return tx_sig
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"[KOL-SNIPE] send error: {e}")
        return None

    def _build_swap_tx(self, quote) -> Optional[bytes]:
        try:
            payload = {
                "quoteResponse": quote,
                "userPublicKey": self.executor.public_key_str,
                "wrapAndUnwrapSol": True,
                "computeUnitPriceMicroLamports": int(
                    self.executor.priority_fee_microlamports * self.config.priority_fee_multiplier
                ),
                "asLegacyTransaction": False,
            }
            if self.executor.compute_unit_limit > 0:
                payload["computeUnitLimit"] = self.executor.compute_unit_limit
            resp = requests.post("https://quote-api.jup.ag/v6/swap", json=payload, timeout=3)
            if resp.status_code != 200:
                return None
            swap_data = resp.json()
            tx_b64 = swap_data.get("swapTransaction")
            if not tx_b64:
                return None
            return base64.b64decode(tx_b64)
        except Exception as e:
            logger.error(f"[KOL-SNIPE] build error: {e}")
            return None

    def _calc_amount(self, kol_buy_sol: float) -> float:
        amt = max(self.config.min_amount_sol, min(self.config.fixed_amount_sol, self.config.max_amount_sol))
        # Optionally match 10% of KOL buy capped
        pct = kol_buy_sol * 0.1
        amt = min(self.config.max_amount_sol, max(self.config.min_amount_sol, pct, amt))
        return amt

    async def _execute_snipe_for_token(self, token_mint: str, amount_sol: float, reason: str = "bundle") -> Optional[str]:
        """
        Shared fast-path execution for bundle snipes.
        """
        quote = self.executor._get_quote_full(
            token_mint=token_mint,
            slippage_bps=self.config.slippage_bps,
            amount_sol=amount_sol,
            priority_fee=int(self.executor.priority_fee_microlamports * self.config.priority_fee_multiplier),
        )
        if not quote:
            return None
        return await self._send_fast(quote)

    def _record_position(self, token_mint: str, amount_sol: float, sig: str, source: str, source_details: Optional[dict] = None):
        """
        Add a tracked position via the executor's PositionManager.
        """
        if not getattr(self.executor, "position_manager", None):
            return
        try:
            td = dexscreener.get_token_data("solana", token_mint) or {}
            price = td.get("price_usd") or 0
            symbol = td.get("symbol") or token_mint[:6]
            sol_price = self.executor._approx_sol_usd()
            entry_tokens = 0.0
            if price and sol_price:
                entry_tokens = (amount_sol * sol_price) / price
            pos = self.executor.position_manager.add_position(
                token_mint=token_mint,
                token_symbol=symbol,
                entry_signature=sig,
                entry_slot=0,
                entry_price_usd=price or 0,
                entry_amount_sol=amount_sol,
                entry_amount_tokens=entry_tokens,
                source=source,
                source_details=source_details or {},
            )
            metrics_collector.position_set(token_mint, amount_sol)
            logger.info(f"[KOL-SNIPE] Position tracked {pos.id} {symbol} size {amount_sol:.4f} SOL")
        except Exception as e:
            logger.error(f"[KOL-SNIPE] position tracking error: {e}")

    def _alert(self, event: KOLBuyEvent, amount_sol: float, sig: Optional[str], latency_ms: float, success: bool):
        chat_id = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")
        if not chat_id or not telegram_bot.enabled:
            return
        if success:
            msg = (
                "🎯 <b>KOL SNIPE SUCCESS</b>\n\n"
                f"<b>KOL:</b> {event.kol_name}\n"
                f"<b>Token:</b> <code>{event.token_mint[:12]}...</code>\n"
                f"<b>KOL Buy:</b> {event.amount_sol:.3f} SOL\n"
                f"<b>Our Buy:</b> {amount_sol:.3f} SOL\n"
                f"<b>DEX:</b> {event.dex}\n"
                f"<b>Latency:</b> {latency_ms:.0f}ms\n"
                f"<a href='https://solscan.io/tx/{sig}'>View TX</a>"
            )
        else:
            msg = (
                "❌ <b>KOL SNIPE FAILED</b>\n\n"
                f"<b>KOL:</b> {event.kol_name}\n"
                f"<b>Token:</b> <code>{event.token_mint[:12]}...</code>\n"
                f"<b>Attempted:</b> {amount_sol:.3f} SOL\n"
                f"<b>Latency:</b> {latency_ms:.0f}ms"
            )
        try:
            telegram_bot.send_message(chat_id, msg)
        except Exception:
            pass

