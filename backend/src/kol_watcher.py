"""
KOL wallet watcher: subscribes to transactions of allowlisted wallets and emits buy events.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Callable, Dict, Optional, Set

import websockets
from solders.transaction import VersionedTransaction

logger = logging.getLogger(__name__)


@dataclass
class KOLBuyEvent:
    kol_wallet: str
    kol_name: str
    token_mint: str
    amount_sol: float
    signature: str
    slot: int
    timestamp: datetime
    dex: str  # pump|raydium|jupiter|unknown
    detected_at_ms: float


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM_AMM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


class KOLWatcher:
    def __init__(
        self,
        kol_wallets: Dict[str, str],
        on_kol_buy: Callable[[KOLBuyEvent], None],
        geyser_url: Optional[str] = None,
        geyser_token: Optional[str] = None,
    ):
        self.kol_wallets = kol_wallets
        self.on_kol_buy = on_kol_buy
        self.geyser_url = geyser_url or os.getenv("GEYSER_WS_URL", "")
        self.geyser_token = geyser_token or os.getenv("GEYSER_TOKEN", "")
        self._ws = None
        self._running = False
        self._seen_sigs: Set[str] = set()
        self.events_detected = 0
        self.buys_detected = 0
        self.avg_detection_latency_ms = 0.0

    async def start(self):
        if not self.geyser_url:
            logger.error("[KOL] No Geyser URL configured")
            return
        if not self.kol_wallets:
            logger.warning("[KOL] No KOL wallets configured")
            return
        self._running = True
        logger.info(f"[KOL] Watching {len(self.kol_wallets)} wallets via {self.geyser_url[:60]}...")
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.error(f"[KOL] Connection error: {e}")
                await asyncio.sleep(2)

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _connect_and_listen(self):
        headers = {}
        if self.geyser_token:
            headers["Authorization"] = f"Bearer {self.geyser_token}"
        async with websockets.connect(
            self.geyser_url,
            extra_headers=headers,
            ping_interval=20,
            ping_timeout=10,
            max_size=10_000_000,
        ) as ws:
            self._ws = ws
            logger.info("[KOL] Connected to Geyser")
            await self._subscribe(ws)
            async for message in ws:
                if not self._running:
                    break
                start = perf_counter()
                try:
                    await self._handle_message(message, start)
                except Exception as e:
                    logger.debug(f"[KOL] Message error: {e}")

    async def _subscribe(self, ws):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "transactionSubscribe",
            "params": [
                {"accountInclude": list(self.kol_wallets.keys()), "failed": False},
                {
                    "commitment": "processed",
                    "encoding": "base64",
                    "transactionDetails": "full",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }
        await ws.send(json.dumps(msg))
        logger.info(f"[KOL] Subscribed to {len(self.kol_wallets)} wallets")

    async def _handle_message(self, message: str, start: float):
        self.events_detected += 1
        try:
            data = json.loads(message)
        except Exception:
            return
        tx_data = self._extract_tx(data)
        if not tx_data:
            return
        sig = tx_data.get("signature", "")
        if sig in self._seen_sigs:
            return
        self._seen_sigs.add(sig)
        if len(self._seen_sigs) > 5000:
            self._seen_sigs = set(list(self._seen_sigs)[-2500:])
        evt = self._parse_buy(tx_data, start)
        if evt:
            self.buys_detected += 1
            latency_ms = (perf_counter() - start) * 1000
            self._update_latency(latency_ms)
            logger.info(
                f"[KOL] 🎯 {evt.kol_name} {evt.token_mint[:8]}... {evt.amount_sol:.3f} SOL | {evt.dex} | {latency_ms:.1f}ms"
            )
            asyncio.create_task(self._trigger(evt))

    def _extract_tx(self, data: dict) -> Optional[dict]:
        if "params" in data:
            result = data["params"].get("result", {})
            return result.get("transaction", result)
        if "result" in data and isinstance(data["result"], dict):
            return data["result"]
        return None

    def _parse_buy(self, tx_data: dict, start: float) -> Optional[KOLBuyEvent]:
        try:
            sig = tx_data.get("signature", "")
            slot = tx_data.get("slot", 0)
            tx_raw = tx_data.get("transaction", {})
            meta = tx_data.get("meta", {}) or {}
            if isinstance(tx_raw, str):
                tx_bytes = base64.b64decode(tx_raw)
                tx = VersionedTransaction.from_bytes(tx_bytes)
                msg = tx.message
                account_keys = [str(k) for k in msg.account_keys]
            elif isinstance(tx_raw, dict):
                msg = tx_raw.get("message", {}) or {}
                account_keys = msg.get("accountKeys") or msg.get("staticAccountKeys") or []
            else:
                return None
            kol = None
            kol_name = None
            for addr in account_keys[:5]:
                a = str(addr)
                if a in self.kol_wallets:
                    kol = a
                    kol_name = self.kol_wallets[a]
                    break
            if not kol:
                return None
            dex = "unknown"
            for addr in account_keys:
                a = str(addr)
                if a == PUMP_PROGRAM:
                    dex = "pump"
                elif a == RAYDIUM_AMM:
                    dex = "raydium"
                elif a == JUPITER_V6:
                    dex = "jupiter"
            post_bal = meta.get("postTokenBalances", []) or []
            pre_bal = meta.get("preTokenBalances", []) or []
            token_mint = None
            for post in post_bal:
                if post.get("owner") == kol:
                    mint = post.get("mint")
                    if mint and mint != "So11111111111111111111111111111111111111112":
                        pre_amt = 0
                        for pre in pre_bal:
                            if pre.get("mint") == mint and pre.get("owner") == kol:
                                pre_amt = float(pre.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                                break
                        post_amt = float(post.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                        if post_amt > pre_amt:
                            token_mint = mint
                            break
            if not token_mint:
                return None
            amount_sol = 0.0
            pre_sol = meta.get("preBalances") or []
            post_sol = meta.get("postBalances") or []
            try:
                idx = account_keys.index(kol)
                if idx < len(pre_sol) and idx < len(post_sol):
                    diff = (pre_sol[idx] - post_sol[idx]) / 1e9
                    if diff > 0:
                        amount_sol = diff
            except Exception:
                pass
            return KOLBuyEvent(
                kol_wallet=kol,
                kol_name=kol_name or kol,
                token_mint=token_mint,
                amount_sol=amount_sol,
                signature=sig,
                slot=slot,
                timestamp=datetime.now(),
                dex=dex,
                detected_at_ms=start,
            )
        except Exception as e:
            logger.debug(f"[KOL] parse error: {e}")
            return None

    async def _trigger(self, event: KOLBuyEvent):
        try:
            if asyncio.iscoroutinefunction(self.on_kol_buy):
                await self.on_kol_buy(event)
            else:
                self.on_kol_buy(event)
        except Exception as e:
            logger.error(f"[KOL] callback error: {e}")

    def _update_latency(self, latency_ms: float):
        if self.avg_detection_latency_ms == 0:
            self.avg_detection_latency_ms = latency_ms
        else:
            self.avg_detection_latency_ms = self.avg_detection_latency_ms * 0.9 + latency_ms * 0.1

