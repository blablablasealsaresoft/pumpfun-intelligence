"""
Universal bundle/launch detector via Geyser firehose.
Detects new pools, pump graduations, whale/coordinated buys.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Callable, Dict, List, Optional, Set

import websockets

logger = logging.getLogger(__name__)

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM_AMM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
ORCA_WHIRLPOOL = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
WSOL = "So11111111111111111111111111111111111111112"


@dataclass
class BundleLaunchEvent:
    token_mint: str
    event_type: str  # new_pool | pump_graduation | whale_entry | coordinated_buy
    slot: int
    first_signature: str
    signatures: List[str] = field(default_factory=list)
    num_buyers: int = 0
    total_sol_volume: float = 0.0
    buyer_wallets: List[str] = field(default_factory=list)
    pool_address: Optional[str] = None
    initial_liquidity_sol: float = 0.0
    dex: str = ""
    detected_at: datetime = field(default_factory=datetime.now)
    detection_latency_ms: float = 0.0
    confidence: float = 0.0


@dataclass
class SlotActivity:
    slot: int
    token_buys: Dict[str, List[dict]] = field(default_factory=lambda: defaultdict(list))
    new_pools: List[dict] = field(default_factory=list)
    whale_buys: List[dict] = field(default_factory=list)
    fresh_wallets: Set[str] = field(default_factory=set)
    signatures: Set[str] = field(default_factory=set)


class BundleDetector:
    def __init__(
        self,
        on_launch_detected: Callable[[BundleLaunchEvent], None],
        geyser_url: Optional[str] = None,
        geyser_token: Optional[str] = None,
    ):
        self.on_launch_detected = on_launch_detected
        self.geyser_url = geyser_url or os.getenv("GEYSER_WS_URL", "")
        self.geyser_token = geyser_token or os.getenv("GEYSER_TOKEN", "")

        self.min_coordinated_buyers = int(os.getenv("MIN_COORDINATED_BUYERS", "3"))
        self.whale_threshold_sol = float(os.getenv("WHALE_BUY_THRESHOLD_SOL", "10.0"))
        self.min_pool_liquidity_sol = float(os.getenv("MIN_POOL_LIQUIDITY_SOL", "1.0"))
        self.slot_window = int(os.getenv("BUNDLE_SLOT_WINDOW", "2"))

        self._ws = None
        self._running = False
        self._seen_sigs: Set[str] = set()
        self._seen_tokens: Set[str] = set()
        self._slot_activity: Dict[int, SlotActivity] = {}
        self._current_slot = 0
        self._wallet_first_seen: Dict[str, int] = {}

        self.events_processed = 0
        self.launches_detected = 0
        self.avg_detection_latency_ms = 0.0

    async def start(self):
        if not self.geyser_url:
            logger.error("[BUNDLE] No Geyser URL configured")
            return
        self._running = True
        logger.info(
            f"[BUNDLE] Starting detector (min_buyers={self.min_coordinated_buyers}, whale={self.whale_threshold_sol} SOL)"
        )
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.error(f"[BUNDLE] Connection error: {e}")
                await asyncio.sleep(1)

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
            max_size=50_000_000,
        ) as ws:
            self._ws = ws
            logger.info("[BUNDLE] Connected to Geyser firehose")
            await self._subscribe_firehose(ws)
            async for message in ws:
                if not self._running:
                    break
                start = perf_counter()
                try:
                    await self._handle_message(message, start)
                except Exception as e:
                    logger.debug(f"[BUNDLE] Message error: {e}")

    async def _subscribe_firehose(self, ws):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "transactionSubscribe",
            "params": [
                {
                    "accountInclude": [PUMP_PROGRAM, RAYDIUM_AMM, JUPITER_V6, ORCA_WHIRLPOOL],
                    "failed": False,
                },
                {
                    "commitment": "processed",
                    "encoding": "base64",
                    "transactionDetails": "full",
                    "showRewards": False,
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }
        await ws.send(json.dumps(msg))
        logger.info("[BUNDLE] Subscribed to DEX programs")

    async def _handle_message(self, message: str, start: float):
        self.events_processed += 1
        try:
            data = json.loads(message)
        except Exception:
            return
        tx_data = self._extract_tx(data)
        if not tx_data:
            return
        sig = tx_data.get("signature", "")
        slot = tx_data.get("slot", 0)
        if sig in self._seen_sigs:
            return
        self._seen_sigs.add(sig)
        if len(self._seen_sigs) > 20000:
            self._seen_sigs = set(list(self._seen_sigs)[-10000:])
        if slot > self._current_slot:
            await self._process_completed_slots(slot)
            self._current_slot = slot
        parsed = self._parse_tx(tx_data)
        if not parsed:
            return
        self._add_activity(slot, parsed, sig)
        evt = self._immediate_triggers(parsed, slot, sig, start)
        if evt:
            await self._emit(evt)

    def _extract_tx(self, data: dict) -> Optional[dict]:
        if "params" in data:
            result = data["params"].get("result", {})
            return result.get("transaction", result)
        if "result" in data and isinstance(data["result"], dict):
            return data["result"]
        return None

    def _parse_tx(self, tx_data: dict) -> Optional[dict]:
        try:
            sig = tx_data.get("signature", "")
            slot = tx_data.get("slot", 0)
            meta = tx_data.get("meta", {}) or {}
            tx_raw = tx_data.get("transaction", {})
            if isinstance(tx_raw, str):
                tx_bytes = base64.b64decode(tx_raw)
                tx = VersionedTransaction.from_bytes(tx_bytes)
                account_keys = [str(k) for k in tx.message.account_keys]
            elif isinstance(tx_raw, dict):
                msg = tx_raw.get("message", {}) or {}
                account_keys = msg.get("accountKeys") or msg.get("staticAccountKeys") or []
            else:
                return None
            dex = None
            for a in account_keys:
                if a == PUMP_PROGRAM:
                    dex = "pump"
                elif a == RAYDIUM_AMM:
                    dex = "raydium"
                elif a == JUPITER_V6:
                    dex = "jupiter"
                elif a == ORCA_WHIRLPOOL:
                    dex = "orca"
            if not dex:
                return None

            is_new_pool = False
            logs = meta.get("logMessages") or []
            for log in logs:
                l = log.lower()
                if "initializepool" in l or "initialize" in l:
                    is_new_pool = True
                    break
                if "migrate" in l and dex == "pump":
                    is_new_pool = True
                    dex = "pump_graduation"
                    break

            token_mint = None
            buyer = account_keys[0] if account_keys else None
            post_bal = meta.get("postTokenBalances", []) or []
            pre_bal = meta.get("preTokenBalances", []) or []
            for post in post_bal:
                if post.get("owner") == buyer:
                    mint = post.get("mint")
                    if mint and mint != WSOL:
                        pre_amt = 0
                        for pre in pre_bal:
                            if pre.get("mint") == mint and pre.get("owner") == buyer:
                                pre_amt = float(pre.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                                break
                        post_amt = float(post.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                        if post_amt > pre_amt:
                            token_mint = mint
                            break
            if not token_mint:
                return None

            pre_sol = meta.get("preBalances") or []
            post_sol = meta.get("postBalances") or []
            amount_sol = 0.0
            if pre_sol and post_sol and len(pre_sol) > 0 and len(post_sol) > 0:
                diff = (pre_sol[0] - post_sol[0]) / 1e9
                if diff > 0:
                    amount_sol = diff

            if buyer and buyer not in self._wallet_first_seen:
                self._wallet_first_seen[buyer] = slot
            is_fresh = (slot - self._wallet_first_seen.get(buyer, slot)) < 100

            return {
                "signature": sig,
                "slot": slot,
                "dex": dex,
                "buyer": buyer,
                "token_mint": token_mint,
                "amount_sol": amount_sol,
                "is_new_pool": is_new_pool,
                "is_fresh_wallet": is_fresh,
            }
        except Exception as e:
            logger.debug(f"[BUNDLE] parse error: {e}")
            return None

    def _add_activity(self, slot: int, parsed: dict, sig: str):
        if slot not in self._slot_activity:
            self._slot_activity[slot] = SlotActivity(slot=slot)
        act = self._slot_activity[slot]
        act.signatures.add(sig)
        token = parsed.get("token_mint")
        if not token:
            return
        act.token_buys[token].append(parsed)
        if parsed["amount_sol"] >= self.whale_threshold_sol:
            act.whale_buys.append(parsed)
        if parsed.get("is_fresh_wallet"):
            act.fresh_wallets.add(parsed["buyer"])
        if parsed.get("is_new_pool"):
            act.new_pools.append(parsed)

    def _immediate_triggers(self, parsed: dict, slot: int, sig: str, start: float) -> Optional[BundleLaunchEvent]:
        token = parsed.get("token_mint")
        if not token or token in self._seen_tokens:
            return None
        if parsed.get("is_new_pool"):
            self._remember_token(token)
            return BundleLaunchEvent(
                token_mint=token,
                event_type="pump_graduation" if parsed["dex"] == "pump_graduation" else "new_pool",
                slot=slot,
                first_signature=sig,
                signatures=[sig],
                num_buyers=1,
                total_sol_volume=parsed["amount_sol"],
                buyer_wallets=[parsed["buyer"]] if parsed["buyer"] else [],
                dex=parsed["dex"],
                detection_latency_ms=(perf_counter() - start) * 1000,
                confidence=0.9,
            )
        if parsed["amount_sol"] >= self.whale_threshold_sol:
            self._remember_token(token)
            return BundleLaunchEvent(
                token_mint=token,
                event_type="whale_entry",
                slot=slot,
                first_signature=sig,
                signatures=[sig],
                num_buyers=1,
                total_sol_volume=parsed["amount_sol"],
                buyer_wallets=[parsed["buyer"]] if parsed["buyer"] else [],
                dex=parsed["dex"],
                detection_latency_ms=(perf_counter() - start) * 1000,
                confidence=0.7,
            )
        return None

    async def _process_completed_slots(self, current_slot: int):
        done_slots = [s for s in self._slot_activity.keys() if s < current_slot - self.slot_window]
        for slot in done_slots:
            act = self._slot_activity.pop(slot, None)
            if not act:
                continue
            for token, buys in act.token_buys.items():
                if token in self._seen_tokens:
                    continue
                unique_buyers = set(b.get("buyer") for b in buys if b.get("buyer"))
                if len(unique_buyers) >= self.min_coordinated_buyers:
                    self._remember_token(token)
                    total_sol = sum(b.get("amount_sol", 0) for b in buys)
                    evt = BundleLaunchEvent(
                        token_mint=token,
                        event_type="coordinated_buy",
                        slot=slot,
                        first_signature=buys[0]["signature"],
                        signatures=[b["signature"] for b in buys if b.get("signature")],
                        num_buyers=len(unique_buyers),
                        total_sol_volume=total_sol,
                        buyer_wallets=list(unique_buyers),
                        dex=buys[0].get("dex", ""),
                        detection_latency_ms=0,
                        confidence=min(0.5 + (len(unique_buyers) * 0.1), 0.95),
                    )
                    await self._emit(evt)

        if len(self._wallet_first_seen) > 50000:
            cutoff = current_slot - 10000
            self._wallet_first_seen = {k: v for k, v in self._wallet_first_seen.items() if v > cutoff}

    def _remember_token(self, token: str):
        self._seen_tokens.add(token)
        if len(self._seen_tokens) > 2000:
            self._seen_tokens = set(list(self._seen_tokens)[-1000:])

    async def _emit(self, event: BundleLaunchEvent):
        self.launches_detected += 1
        try:
            if asyncio.iscoroutinefunction(self.on_launch_detected):
                await self.on_launch_detected(event)
            else:
                self.on_launch_detected(event)
        except Exception as e:
            logger.error(f"[BUNDLE] Callback error: {e}")

    def get_stats(self) -> dict:
        return {
            "events_processed": self.events_processed,
            "launches_detected": self.launches_detected,
            "tokens_seen": len(self._seen_tokens),
            "wallets_tracked": len(self._wallet_first_seen),
        }

