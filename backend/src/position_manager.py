"""
Position tracking and automated exits (TP/SL/Trailing/Time/Rug).
"""

from __future__ import annotations

import asyncio
import json
import base64
import os
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from time import perf_counter
from typing import Optional, Dict, List, Callable

import aiohttp
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

logger = logging.getLogger(__name__)


class ExitReason(Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    TIME_EXIT = "time_exit"
    MANUAL = "manual"
    RUG_DETECTED = "rug_detected"


@dataclass
class Position:
    id: str
    token_mint: str
    token_symbol: str
    entry_signature: str
    entry_slot: int
    entry_time: datetime
    entry_price_usd: float
    entry_amount_sol: float
    entry_amount_tokens: float
    source: str
    source_details: dict = field(default_factory=dict)
    current_price_usd: float = 0.0
    current_value_sol: float = 0.0
    highest_price_usd: float = 0.0
    lowest_price_usd: float = 0.0
    unrealized_pnl_pct: float = 0.0
    unrealized_pnl_sol: float = 0.0
    take_profit_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    trailing_stop_pct: Optional[float] = None
    trailing_stop_activation_pct: Optional[float] = None
    max_hold_minutes: Optional[int] = None
    trailing_stop_active: bool = False
    trailing_stop_price: float = 0.0
    exit_signature: Optional[str] = None
    exit_time: Optional[datetime] = None
    exit_price_usd: Optional[float] = None
    exit_reason: Optional[ExitReason] = None
    realized_pnl_sol: Optional[float] = None

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    @property
    def hold_duration_minutes(self) -> float:
        end_time = self.exit_time or datetime.now()
        return (end_time - self.entry_time).total_seconds() / 60

    def update_price(self, price_usd: float, sol_price_usd: float):
        self.current_price_usd = price_usd
        self.last_update = datetime.now()
        if price_usd > self.highest_price_usd:
            self.highest_price_usd = price_usd
        if price_usd < self.lowest_price_usd or self.lowest_price_usd == 0:
            self.lowest_price_usd = price_usd
        if self.entry_price_usd > 0 and sol_price_usd > 0:
            current_value_usd = self.entry_amount_tokens * price_usd
            self.current_value_sol = current_value_usd / sol_price_usd
            self.unrealized_pnl_pct = ((price_usd / self.entry_price_usd) - 1) * 100
            self.unrealized_pnl_sol = self.current_value_sol - self.entry_amount_sol

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_time"] = self.entry_time.isoformat()
        if self.exit_time:
            d["exit_time"] = self.exit_time.isoformat()
        if self.exit_reason:
            d["exit_reason"] = self.exit_reason.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        d["entry_time"] = datetime.fromisoformat(d["entry_time"])
        if d.get("exit_time"):
            d["exit_time"] = datetime.fromisoformat(d["exit_time"])
        if d.get("exit_reason"):
            d["exit_reason"] = ExitReason(d["exit_reason"])
        return cls(**d)


@dataclass
class ExitConfig:
    take_profit_pct: float = 75.0
    take_profit_partial_pct: float = 50.0
    take_profit_partial_at_pct: float = 50.0
    enable_partial_tp: bool = True
    stop_loss_pct: float = 15.0
    trailing_stop_pct: float = 10.0
    trailing_stop_activation_pct: float = 20.0
    enable_trailing_stop: bool = True
    max_hold_minutes: int = 60
    enable_time_exit: bool = True
    rug_drop_pct: float = 35.0
    rug_liquidity_threshold_usd: float = 2000.0
    enable_rug_protection: bool = True
    sell_slippage_bps: int = 1000
    sell_priority_fee_multiplier: float = 2.0
    use_jito_for_exits: bool = True
    jito_tip_lamports: int = 150000
    price_poll_seconds: int = 5

    @classmethod
    def from_env(cls) -> "ExitConfig":
        return cls(
            take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "75")),
            take_profit_partial_pct=float(os.getenv("TAKE_PROFIT_PARTIAL_PCT", "50")),
            take_profit_partial_at_pct=float(os.getenv("TAKE_PROFIT_PARTIAL_AT_PCT", "50")),
            enable_partial_tp=os.getenv("ENABLE_PARTIAL_TP", "true").lower() == "true",
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "15")),
            trailing_stop_pct=float(os.getenv("TRAILING_STOP_PCT", "10")),
            trailing_stop_activation_pct=float(os.getenv("TRAILING_STOP_ACTIVATION_PCT", "20")),
            enable_trailing_stop=os.getenv("ENABLE_TRAILING_STOP", "true").lower() == "true",
            max_hold_minutes=int(os.getenv("MAX_HOLD_MINUTES", "60")),
            enable_time_exit=os.getenv("ENABLE_TIME_EXIT", "true").lower() == "true",
            rug_drop_pct=float(os.getenv("RUG_DROP_PCT", "35")),
            rug_liquidity_threshold_usd=float(os.getenv("RUG_LIQ_THRESHOLD_USD", "2000")),
            enable_rug_protection=os.getenv("ENABLE_RUG_PROTECTION", "true").lower() == "true",
            sell_slippage_bps=int(os.getenv("SELL_SLIPPAGE_BPS", "1000")),
            sell_priority_fee_multiplier=float(os.getenv("SELL_PRIORITY_FEE_MULTIPLIER", "2.0")),
            use_jito_for_exits=os.getenv("USE_JITO_FOR_EXITS", "true").lower() == "true",
            jito_tip_lamports=int(os.getenv("EXIT_JITO_TIP_LAMPORTS", "150000")),
            price_poll_seconds=int(os.getenv("PRICE_POLL_SECONDS", "5")),
        )


class PositionManager:
    def __init__(
        self,
        rpc_client: AsyncClient,
        keypair: Keypair,
        config: Optional[ExitConfig] = None,
        on_exit: Optional[Callable[[Position, ExitReason], None]] = None,
    ):
        self.rpc = rpc_client
        self.keypair = keypair
        self.wallet = self.keypair.pubkey()
        self.config = config or ExitConfig.from_env()
        self.on_exit = on_exit
        self.positions: Dict[str, Position] = {}
        self.positions_by_mint: Dict[str, str] = {}
        self._price_cache: Dict[str, tuple] = {}
        self._sol_price_usd: float = 0.0
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._positions_file = Path(os.getenv("POSITIONS_FILE", "data/positions.json"))
        self._positions_file.parent.mkdir(parents=True, exist_ok=True)
        self.jupiter_url = os.getenv("JUPITER_API_URL", "https://quote-api.jup.ag/v6")
        self.dexscreener_url = "https://api.dexscreener.com/latest/dex"
        self.jito_url = os.getenv("JITO_BLOCK_ENGINE_URL", "https://mainnet.block-engine.jito.wtf/api/v1/bundles")
        self.exits_executed = 0
        self.total_realized_pnl_sol = 0.0
        self._load_positions()

    async def start(self):
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"[POSITIONS] Started monitoring {len(self.positions)} positions")

    async def stop(self):
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
        self._save_positions()

    def add_position(
        self,
        token_mint: str,
        token_symbol: str,
        entry_signature: str,
        entry_slot: int,
        entry_price_usd: float,
        entry_amount_sol: float,
        entry_amount_tokens: float,
        source: str,
        source_details: Optional[dict] = None,
        custom_exits: Optional[dict] = None,
    ) -> Position:
        position_id = f"{token_mint[:8]}-{entry_slot}"
        position = Position(
            id=position_id,
            token_mint=token_mint,
            token_symbol=token_symbol,
            entry_signature=entry_signature,
            entry_slot=entry_slot,
            entry_time=datetime.now(),
            entry_price_usd=entry_price_usd,
            entry_amount_sol=entry_amount_sol,
            entry_amount_tokens=entry_amount_tokens,
            source=source,
            source_details=source_details or {},
            current_price_usd=entry_price_usd,
            highest_price_usd=entry_price_usd,
            lowest_price_usd=entry_price_usd,
        )
        if custom_exits:
            position.take_profit_pct = custom_exits.get("take_profit_pct")
            position.stop_loss_pct = custom_exits.get("stop_loss_pct")
            position.trailing_stop_pct = custom_exits.get("trailing_stop_pct")
            position.trailing_stop_activation_pct = custom_exits.get("trailing_stop_activation_pct")
            position.max_hold_minutes = custom_exits.get("max_hold_minutes")
        self.positions[position_id] = position
        self.positions_by_mint[token_mint] = position_id
        self._save_positions()
        return position

    async def close_position(self, position_id: str, reason: ExitReason, exit_price_usd: Optional[float] = None) -> Optional[str]:
        position = self.positions.get(position_id)
        if not position or not position.is_open:
            return None
        sig = await self._execute_sell(position)
        if sig:
            position.exit_signature = sig
            position.exit_time = datetime.now()
            position.exit_price_usd = exit_price_usd or position.current_price_usd
            position.exit_reason = reason
            position.realized_pnl_sol = position.unrealized_pnl_sol
            self.exits_executed += 1
            self.total_realized_pnl_sol += position.realized_pnl_sol
            self.positions_by_mint.pop(position.token_mint, None)
            self._save_positions()
            if self.on_exit:
                try:
                    if asyncio.iscoroutinefunction(self.on_exit):
                        await self.on_exit(position, reason)
                    else:
                        self.on_exit(position, reason)
                except Exception as e:
                    logger.error(f"[POSITIONS] Exit callback error: {e}")
        return sig

    async def _monitor_loop(self):
        while self._running:
            try:
                await self._check_all_positions()
            except Exception as e:
                logger.error(f"[POSITIONS] Monitor error: {e}")
            await asyncio.sleep(self.config.price_poll_seconds)

    async def _check_all_positions(self):
        await self._update_sol_price()
        open_positions = [p for p in self.positions.values() if p.is_open]
        if not open_positions:
            return
        mints = [p.token_mint for p in open_positions]
        prices = await self._fetch_prices_batch(mints)
        for position in open_positions:
            price = prices.get(position.token_mint)
            if price is None:
                continue
            position.update_price(price, self._sol_price_usd)
            reason = self._check_exit_conditions(position)
            if reason:
                await self.close_position(position.id, reason, price)

    def _check_exit_conditions(self, position: Position) -> Optional[ExitReason]:
        tp_pct = position.take_profit_pct or self.config.take_profit_pct
        sl_pct = position.stop_loss_pct or self.config.stop_loss_pct
        ts_pct = position.trailing_stop_pct or self.config.trailing_stop_pct
        ts_activation = position.trailing_stop_activation_pct or self.config.trailing_stop_activation_pct
        max_hold = position.max_hold_minutes or self.config.max_hold_minutes
        pnl_pct = position.unrealized_pnl_pct

        if self.config.enable_rug_protection and pnl_pct <= -self.config.rug_drop_pct:
            return ExitReason.RUG_DETECTED
        if pnl_pct <= -sl_pct:
            return ExitReason.STOP_LOSS
        if pnl_pct >= tp_pct:
            return ExitReason.TAKE_PROFIT
        if self.config.enable_trailing_stop:
            if pnl_pct >= ts_activation and not position.trailing_stop_active:
                position.trailing_stop_active = True
                position.trailing_stop_price = position.current_price_usd * (1 - ts_pct / 100)
            if position.trailing_stop_active:
                new_stop = position.highest_price_usd * (1 - ts_pct / 100)
                if new_stop > position.trailing_stop_price:
                    position.trailing_stop_price = new_stop
                if position.current_price_usd <= position.trailing_stop_price:
                    return ExitReason.TRAILING_STOP
        if self.config.enable_time_exit and position.hold_duration_minutes >= max_hold:
            return ExitReason.TIME_EXIT
        return None

    async def _execute_sell(self, position: Position) -> Optional[str]:
        try:
            # Try Jito via Jupiter build
            sig = await self._sell_via_jito(position)
            if sig:
                return sig
            return await self._sell_via_jupiter(position)
        except Exception as e:
            logger.error(f"[POSITIONS] Sell error: {e}")
            return None

    async def _sell_via_jito(self, position: Position) -> Optional[str]:
        try:
            swap_tx = await self._build_sell_tx(position)
            if not swap_tx:
                return None
            import random
            from solders.message import MessageV0
            from solders.instruction import Instruction
            from solders.hash import Hash

            resp = await self.rpc.get_latest_blockhash()
            blockhash = resp.value.blockhash
            tip_accounts = [
                "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
                "HFqU5x63VTqvQss8hp11i4bVxUg2gAAKJcUTW4zdBrx",
            ]
            tip_account = Pubkey.from_string(random.choice(tip_accounts))
            tip_lamports = self.config.jito_tip_lamports
            tip_data = bytes([2, 0, 0, 0]) + tip_lamports.to_bytes(8, "little")
            tip_ix = Instruction(
                program_id=Pubkey.from_string("11111111111111111111111111111111"),
                accounts=[
                    {"pubkey": self.wallet, "is_signer": True, "is_writable": True},
                    {"pubkey": tip_account, "is_signer": False, "is_writable": True},
                ],
                data=tip_data,
            )
            tip_msg = MessageV0.try_compile(
                payer=self.wallet,
                instructions=[tip_ix],
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tip_tx = VersionedTransaction(tip_msg, [self.keypair])
            bundle = [
                base64.b64encode(swap_tx).decode(),
                base64.b64encode(bytes(tip_tx)).decode(),
            ]
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.jito_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "sendBundle", "params": [bundle]},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return result.get("result")
            return None
        except Exception as e:
            logger.error(f"[POSITIONS] Jito sell error: {e}")
            return None

    async def _sell_via_jupiter(self, position: Position) -> Optional[str]:
        try:
            swap_tx = await self._build_sell_tx(position)
            if not swap_tx:
                return None
            resp = await self.rpc.send_raw_transaction(
                swap_tx,
                opts=TxOpts(skip_preflight=True, max_retries=0),
            )
            return str(resp.value) if resp.value else None
        except Exception as e:
            logger.error(f"[POSITIONS] Jupiter sell error: {e}")
            return None

    async def _build_sell_tx(self, position: Position) -> Optional[bytes]:
        try:
            amount = int(position.entry_amount_tokens * (10 ** 6))
            async with aiohttp.ClientSession() as session:
                quote_url = (
                    f"{self.jupiter_url}/quote?"
                    f"inputMint={position.token_mint}&"
                    f"outputMint=So11111111111111111111111111111111111111112&"
                    f"amount={amount}&"
                    f"slippageBps={self.config.sell_slippage_bps}"
                )
                async with session.get(quote_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return None
                    quote = await resp.json()
                priority_fee = int(
                    float(os.getenv("PRIORITY_FEE_MICROLAMPORTS", "50000"))
                    * self.config.sell_priority_fee_multiplier
                )
                swap_body = {
                    "quoteResponse": quote,
                    "userPublicKey": str(self.wallet),
                    "wrapAndUnwrapSol": True,
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": priority_fee,
                }
                async with session.post(
                    f"{self.jupiter_url}/swap",
                    json=swap_body,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return None
                    swap_data = await resp.json()
                tx_bytes = base64.b64decode(swap_data["swapTransaction"])
                tx = VersionedTransaction.from_bytes(tx_bytes)
                tx.sign([self.keypair])
                return bytes(tx)
        except Exception as e:
            logger.error(f"[POSITIONS] Build sell TX error: {e}")
            return None

    async def _fetch_prices_batch(self, mints: List[str]) -> Dict[str, float]:
        prices = {}
        try:
            async with aiohttp.ClientSession() as session:
                for i in range(0, len(mints), 30):
                    batch = mints[i : i + 30]
                    url = f"{self.dexscreener_url}/tokens/{','.join(batch)}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for pair in data.get("pairs", []):
                                mint = pair.get("baseToken", {}).get("address")
                                if mint in batch:
                                    price = float(pair.get("priceUsd", 0) or 0)
                                    if price > 0:
                                        prices[mint] = price
        except Exception:
            pass
        return prices

    async def _update_sol_price(self):
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.dexscreener_url}/tokens/So11111111111111111111111111111111111111112"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pairs = data.get("pairs", [])
                        if pairs:
                            self._sol_price_usd = float(pairs[0].get("priceUsd", 0) or 0)
        except Exception:
            pass
        if self._sol_price_usd == 0:
            self._sol_price_usd = 200.0

    def _save_positions(self):
        try:
            data = {
                "positions": {k: v.to_dict() for k, v in self.positions.items()},
                "stats": {
                    "exits_executed": self.exits_executed,
                    "total_realized_pnl_sol": self.total_realized_pnl_sol,
                },
            }
            with open(self._positions_file, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"[POSITIONS] Save error: {e}")

    def _load_positions(self):
        if not self._positions_file.exists():
            return
        try:
            with open(self._positions_file) as f:
                data = json.load(f)
            for k, v in data.get("positions", {}).items():
                position = Position.from_dict(v)
                self.positions[k] = position
                if position.is_open:
                    self.positions_by_mint[position.token_mint] = k
            stats = data.get("stats", {})
            self.exits_executed = stats.get("exits_executed", 0)
            self.total_realized_pnl_sol = stats.get("total_realized_pnl_sol", 0.0)
            logger.info(f"[POSITIONS] Loaded {len(self.positions)} positions")
        except Exception as e:
            logger.error(f"[POSITIONS] Load error: {e}")

    def get_open_positions(self) -> List[Position]:
        return [p for p in self.positions.values() if p.is_open]

    def get_stats(self) -> dict:
        open_positions = self.get_open_positions()
        total_unrealized = sum(p.unrealized_pnl_sol for p in open_positions)
        return {
            "open_positions": len(open_positions),
            "total_positions": len(self.positions),
            "exits_executed": self.exits_executed,
            "total_realized_pnl_sol": round(self.total_realized_pnl_sol, 4),
            "total_unrealized_pnl_sol": round(total_unrealized, 4),
            "sol_price_usd": round(self._sol_price_usd, 2),
        }


