"""
Geyser websocket watcher for ultra-fast pool creation detection (Item #6).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Set

import websockets

logger = logging.getLogger(__name__)

# Known instruction discriminators (first 8 bytes of IX data)
INSTRUCTION_HASHES = {
    "pump_migrate": os.getenv("PUMP_MIGRATE_IX_HASH", ""),
    "raydium_init": os.getenv("RAYDIUM_INIT_IX_HASH", ""),
    "orca_init": os.getenv("ORCA_INIT_IX_HASH", ""),
}

# Program IDs to monitor
PROGRAM_IDS = {
    "pump": os.getenv("PUMPFUN_PROGRAM_ID", "pump111111111111111111111111111111111111111"),
    "raydium": os.getenv("RAYDIUM_PROGRAM_ID", "RVKd61ztZW9dqrjK5vCZH1vZ1tc665Ar72Xd1LgjAoG"),
    "orca": os.getenv("ORCA_PROGRAM_ID", "9WwN7dBDEuDfSUdifYEYdzSsfXCMVvjJhtCmvYzuq76A"),
}


@dataclass
class NewPoolEvent:
    pool_type: str  # "pump_graduation" | "raydium_create" | "orca_create"
    pool_address: str
    token_mint: str
    base_mint: str
    quote_mint: str
    initial_liquidity_sol: float
    signature: str
    slot: int
    timestamp: datetime
    raw_data: Dict[str, Any]


class GeyserWatcher:
    """
    Watch for new pool creation events via Geyser websocket.
    Triggers callback on detection for immediate sniping.
    """

    def __init__(
        self,
        on_new_pool: Callable[[NewPoolEvent], None],
        geyser_url: Optional[str] = None,
        geyser_token: Optional[str] = None,
    ):
        self.geyser_url = geyser_url or os.getenv("GEYSER_WS_URL", "")
        self.geyser_token = geyser_token or os.getenv("GEYSER_TOKEN", "")
        self.geyser_provider = (os.getenv("GEYSER_PROVIDER", "generic") or "generic").lower()
        self.geyser_api_key = os.getenv("GEYSER_API_KEY", "")
        self.on_new_pool = on_new_pool
        self.enabled = bool(self.geyser_url)
        self.geyser_tx_details = os.getenv("GEYSER_TX_DETAILS", "full").lower()  # full|none
        self.geyser_mode = os.getenv("GEYSER_MODE", "default").lower()  # default|jito

        self._ws = None
        self._running = False
        self._seen_signatures: Set[str] = set()
        self._max_seen = 10000

        # Stats
        self.events_received = 0
        self.pools_detected = 0
        self.last_event_at: Optional[datetime] = None

    async def start(self):
        """Start watching for new pools."""
        if not self.enabled:
            logger.warning("[Geyser] Not configured, skipping")
            return

        self._running = True
        logger.info(f"[Geyser] Connecting to {self.geyser_url[:50]}...")

        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.error(f"[Geyser] Connection error: {e}")
                await asyncio.sleep(5)  # Reconnect delay

    async def stop(self):
        """Stop watching."""
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _connect_and_listen(self):
        """Connect to Geyser and process messages."""
        headers = {}
        url = self.geyser_url

        # Provider-specific auth
        if self.geyser_provider == "helius":
            if "api-key=" not in url and self.geyser_api_key:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}api-key={self.geyser_api_key}"
        elif self.geyser_token:
            headers["Authorization"] = f"Bearer {self.geyser_token}"

        async with websockets.connect(
            url,
            extra_headers=headers,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info("[Geyser] Connected")

            # Subscribe to program transactions
            await self._subscribe(ws)

            async for message in ws:
                if not self._running:
                    break

                try:
                    await self._handle_message(message)
                except Exception as e:
                    logger.error(f"[Geyser] Message handling error: {e}")

    async def _subscribe(self, ws):
        """Subscribe to relevant program activity."""
        if self.geyser_mode == "jito":
            # Jito often prefers block/entry subscriptions; this is a minimal blockSubscribe
            subscribe_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "blockSubscribe",
                "params": [
                    {"mentions": list(PROGRAM_IDS.values())},
                    {"commitment": "processed", "encoding": "json", "maxSupportedTransactionVersion": 0},
                ],
            }
        else:
            subscribe_msg = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "transactionSubscribe",
                "params": [
                    {
                        "accountInclude": list(PROGRAM_IDS.values()),
                        "failed": False,
                    },
                    {
                        "commitment": "processed",
                        "encoding": "base64",
                        "transactionDetails": self.geyser_tx_details,
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            }

        await ws.send(json.dumps(subscribe_msg))
        logger.info(f"[Geyser] Subscribed to {len(PROGRAM_IDS)} programs")

    async def _handle_message(self, message: str):
        """Process incoming Geyser message."""
        self.events_received += 1
        self.last_event_at = datetime.now()

        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        # Extract transaction from notification
        tx_data = self._extract_tx_data(data)
        if not tx_data:
            return

        signature = tx_data.get("signature", "")

        # Dedup
        if signature and signature in self._seen_signatures:
            return
        if signature:
            self._seen_signatures.add(signature)
            if len(self._seen_signatures) > self._max_seen:
                self._seen_signatures = set(list(self._seen_signatures)[-5000:])

        # Check for pool creation instructions
        pool_event = self._parse_pool_creation(tx_data)
        if pool_event:
            self.pools_detected += 1
            logger.info(
                f"[Geyser] 🎯 New pool detected: {pool_event.pool_type} | "
                f"token={pool_event.token_mint[:8]}... | "
                f"liq={pool_event.initial_liquidity_sol:.2f} SOL"
            )

            # Trigger callback (async to not block listener)
            asyncio.create_task(self._trigger_callback(pool_event))

    def _extract_tx_data(self, data: dict) -> Optional[dict]:
        """Extract transaction data from Geyser notification."""
        if "params" in data:
            result = data["params"].get("result", {})
            tx = result.get("transaction") or result.get("value", {}).get("transaction")
            return tx or result
        if "result" in data:
            res = data.get("result")
            if isinstance(res, dict):
                return res.get("transaction") or res.get("value", {}).get("transaction") or res
            return res
        return None

    def _parse_pool_creation(self, tx_data: dict) -> Optional[NewPoolEvent]:
        """Parse transaction to detect pool creation."""
        try:
            signature = tx_data.get("signature", "")
            slot = tx_data.get("slot", 0)

            tx = tx_data.get("transaction", {})
            if isinstance(tx, str):
                tx = self._decode_tx(tx)

            message = tx.get("message", {})
            instructions = message.get("instructions", [])
            account_keys = message.get("accountKeys", [])

            for ix in instructions:
                program_idx = ix.get("programIdIndex", 0)
                if program_idx >= len(account_keys):
                    continue

                program_id = account_keys[program_idx]
                ix_data = ix.get("data", "")

                # Decode instruction data
                if isinstance(ix_data, str):
                    try:
                        ix_bytes = base64.b64decode(ix_data)
                    except Exception:
                        continue
                else:
                    ix_bytes = bytes(ix_data)

                if len(ix_bytes) < 8:
                    continue

                discriminator = ix_bytes[:8].hex()

                pool_type = self._match_instruction(program_id, discriminator)
                if pool_type:
                    accounts = [account_keys[i] for i in ix.get("accounts", []) if i < len(account_keys)]
                    return self._build_pool_event(
                        pool_type=pool_type,
                        accounts=accounts,
                        ix_data=ix_bytes,
                        signature=signature,
                        slot=slot,
                        raw=tx_data,
                    )
            return None
        except Exception as e:
            logger.debug(f"[Geyser] Parse error: {e}")
            return None

    def _match_instruction(self, program_id: str, discriminator: str) -> Optional[str]:
        """Match program + discriminator to pool creation type."""
        if program_id == PROGRAM_IDS.get("pump"):
            if INSTRUCTION_HASHES.get("pump_migrate") and discriminator.startswith(INSTRUCTION_HASHES["pump_migrate"]):
                return "pump_graduation"
        elif program_id == PROGRAM_IDS.get("raydium"):
            if INSTRUCTION_HASHES.get("raydium_init") and discriminator.startswith(INSTRUCTION_HASHES["raydium_init"]):
                return "raydium_create"
        elif program_id == PROGRAM_IDS.get("orca"):
            if INSTRUCTION_HASHES.get("orca_init") and discriminator.startswith(INSTRUCTION_HASHES["orca_init"]):
                return "orca_create"
        return None

    def _build_pool_event(
        self,
        pool_type: str,
        accounts: list,
        ix_data: bytes,
        signature: str,
        slot: int,
        raw: dict,
    ) -> Optional[NewPoolEvent]:
        """Build NewPoolEvent from parsed instruction."""
        try:
            if pool_type == "pump_graduation":
                token_mint = accounts[2] if len(accounts) > 2 else ""
                pool_address = accounts[0] if len(accounts) > 0 else ""
            elif pool_type == "raydium_create":
                pool_address = accounts[0] if len(accounts) > 0 else ""
                token_mint = accounts[8] if len(accounts) > 8 else ""
            elif pool_type == "orca_create":
                pool_address = accounts[0] if len(accounts) > 0 else ""
                token_mint = accounts[2] if len(accounts) > 2 else ""
            else:
                return None

            return NewPoolEvent(
                pool_type=pool_type,
                pool_address=pool_address,
                token_mint=token_mint,
                base_mint=token_mint,
                quote_mint="So11111111111111111111111111111111111111112",
                initial_liquidity_sol=0.0,
                signature=signature,
                slot=slot,
                timestamp=datetime.now(),
                raw_data=raw,
            )
        except Exception as e:
            logger.debug(f"[Geyser] Event build error: {e}")
            return None

    def _decode_tx(self, tx_b64: str) -> dict:
        """Decode base64 transaction. Lightweight parser."""
        try:
            _ = base64.b64decode(tx_b64)
            # For speed we skip deep decode; rely on instruction parsing if present.
            return {"message": {"instructions": [], "accountKeys": []}}
        except Exception:
            return {}

    async def _trigger_callback(self, event: NewPoolEvent):
        """Trigger the on_new_pool callback."""
        try:
            if asyncio.iscoroutinefunction(self.on_new_pool):
                await self.on_new_pool(event)
            else:
                self.on_new_pool(event)
        except Exception as e:
            logger.error(f"[Geyser] Callback error: {e}")

    def get_stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "events_received": self.events_received,
            "pools_detected": self.pools_detected,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "seen_signatures": len(self._seen_signatures),
        }


