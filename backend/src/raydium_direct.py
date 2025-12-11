from __future__ import annotations

import asyncio
import base64
import os
import logging
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Optional, Union

from solana.keypair import Keypair
from solana.publickey import PublicKey
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solders.pubkey import Pubkey

from raydium_direct.pool_parser import fetch_pool_for_mint
from raydium_direct.cache import PoolCache
from raydium_direct.market_parser import parse_market_account, OpenBookMarketState
from raydium_direct.ix_builder import (
    build_swap_transaction,
    ensure_ata_ix,
    get_reserve_mapping,
    get_vault_mapping,
)
from raydium_direct.amm_math import calculate_swap_output, calculate_price_impact
from spl.token.instructions import get_associated_token_address

SOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
logger = logging.getLogger(__name__)


@dataclass
class RaydiumDryRunResult:
    success: bool
    error: Optional[str]
    pool_id: str
    market_id: str
    input_mint: str
    output_mint: str
    amount_in: int
    expected_out: int
    min_out: int
    price_impact_bps: int
    tx_size_bytes: int
    accounts_count: int
    compute_units: int
    priority_fee_microlamports: int
    serialized_tx_base64: str
    serialized_message_base64: str
    pool_cache_hit: bool
    market_cache_hit: bool
    pool_fetch_ms: float
    market_fetch_ms: float
    ix_build_ms: float

    def to_dict(self) -> dict:
        return asdict(self)


class RaydiumDirect:
    def __init__(self, rpc_client: Client, keypair: Keypair):
        self.rpc_client = rpc_client
        self.keypair = keypair
        self.enabled = os.getenv("ENABLE_RAYDIUM_DIRECT", "false").lower() in {"1", "true", "yes", "on"}
        self.timeout_ms = int(os.getenv("DIRECT_DEX_TIMEOUT_MS", "500"))
        ttl_hot = int(os.getenv("RAYDIUM_POOL_CACHE_TTL_MS", "5000"))
        ttl_cold = int(os.getenv("RAYDIUM_POOL_CACHE_TTL_COLD_MS", "30000"))
        self.cache = PoolCache(ttl_ms_hot=ttl_hot, ttl_ms_cold=ttl_cold)
        self.dry_run = os.getenv("RAYDIUM_DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}
        self.priority_fee = int(os.getenv("PRIORITY_FEE_MICROLAMPORTS", "0") or 0)
        self.default_compute_units = int(os.getenv("COMPUTE_UNIT_LIMIT", "200000") or 200000)
        self.fallback_enabled = os.getenv("FALLBACK_TO_JUPITER", "true").lower() in {"1", "true", "yes", "on"}
        self.jupiter_api_url = os.getenv("JUPITER_API_URL", "https://quote-api.jup.ag/v6")

        max_impact_bps = int(os.getenv("MAX_PRICE_IMPACT_BPS", "0") or 0)
        if max_impact_bps == 0:
            max_impact_pct = float(os.getenv("MAX_PRICE_IMPACT_PCT", "0") or 0)
            max_impact_bps = int(max_impact_pct * 100) if max_impact_pct else 0
        self.max_price_impact_bps = max_impact_bps

    async def try_swap(self, token_mint: str, amount_sol: float, slippage_bps: int, priority_fee: int) -> Optional[Union[RaydiumDryRunResult, str]]:
        """
        Attempt a Raydium direct swap.
        Returns:
            - RaydiumDryRunResult if dry_run enabled
            - signature string if broadcasted
            - None on failure (caller should fallback)
        """
        if not self.enabled:
            return None
        amount_in = int(amount_sol * 1_000_000_000)
        input_mint = SOL_MINT
        output_mint = Pubkey.from_string(token_mint)

        dry_run_result = await self.dry_run_swap(
            input_mint=input_mint,
            output_mint=output_mint,
            amount_in=amount_in,
            slippage_bps=slippage_bps,
            priority_fee=priority_fee,
        )
        if not dry_run_result:
            return None

        if self.dry_run:
            self._log_dry_run(dry_run_result)
            return dry_run_result

        tx_bytes = base64.b64decode(dry_run_result.serialized_tx_base64)
        try:
            sig = self.rpc_client.send_raw_transaction(
                tx_bytes,
                opts=TxOpts(skip_preflight=True, preflight_commitment="confirmed"),
            )
        except Exception:
            return None
        return sig.get("result") if isinstance(sig, dict) else getattr(sig, "value", None) or sig

    async def dry_run_swap(
        self,
        input_mint: Pubkey,
        output_mint: Pubkey,
        amount_in: int,
        slippage_bps: int,
        priority_fee: Optional[int] = None,
    ) -> Optional[RaydiumDryRunResult]:
        """
        Build and return a Raydium swap transaction without sending it.
        """
        priority_fee = priority_fee if priority_fee is not None else self.priority_fee

        pool_t0 = perf_counter()
        pool, pool_cache_hit = self._get_pool_for_pair(input_mint, output_mint)
        pool_fetch_ms = (perf_counter() - pool_t0) * 1000
        if not pool or not pool.serum_market:
            return None

        market_t0 = perf_counter()
        market, market_cache_hit = self._get_market(pool.serum_market)
        market_fetch_ms = (perf_counter() - market_t0) * 1000
        if not market:
            return None

        reserves = self._fetch_vault_balances(pool)
        if not reserves:
            return None

        try:
            reserve_in, reserve_out = get_reserve_mapping(pool, input_mint)
            source_vault, dest_vault = get_vault_mapping(pool, input_mint)
        except ValueError:
            return None

        expected_out = calculate_swap_output(amount_in, reserve_in, reserve_out)
        min_out = int(expected_out * (10000 - slippage_bps) / 10000)
        price_impact_bps = int(calculate_price_impact(amount_in, reserve_in, reserve_out) * 10000)
        if self.max_price_impact_bps and price_impact_bps > self.max_price_impact_bps:
            logger.warning(
                f"Price impact {price_impact_bps} bps exceeds max {self.max_price_impact_bps}; aborting direct swap"
            )
            return None

        user_wallet = Pubkey.from_string(str(self.keypair.public_key))
        source_ata, dest_ata = self._resolve_user_atas(user_wallet, input_mint, output_mint)

        create_ata_ix = None
        compute_units = self.default_compute_units
        try:
            dest_ata, create_ata_ix = await ensure_ata_ix(
                rpc_client=self.rpc_client,
                wallet=user_wallet,
                mint=output_mint,
                payer=user_wallet,
            )
            if create_ata_ix:
                compute_units += 30_000  # ATA creation ~25k CU
        except Exception:
            # If ATA check fails, continue without creation to avoid blocking fallback.
            pass

        ix_t0 = perf_counter()
        try:
            blockhash_resp = self.rpc_client.get_latest_blockhash()
            recent_blockhash = (
                blockhash_resp.value.blockhash
                if hasattr(blockhash_resp, "value")
                else blockhash_resp["result"]["value"]["blockhash"]
            )
        except Exception:
            return None

        tx = build_swap_transaction(
            pool_state=pool,
            market_state=market,
            user_wallet=user_wallet,
            user_source_ata=source_ata,
            user_dest_ata=dest_ata,
            amount_in=amount_in,
            min_amount_out=min_out,
            recent_blockhash=recent_blockhash,
            priority_fee_microlamports=priority_fee,
            compute_units=compute_units,
            create_ata_ix=create_ata_ix,
        )

        # Sign for simulation/broadcast readiness
        try:
            tx.sign([self.keypair.to_solders()])
        except Exception:
            return None

        ix_build_ms = (perf_counter() - ix_t0) * 1000
        tx_bytes = bytes(tx)
        message_bytes = bytes(tx.message)

        result = RaydiumDryRunResult(
            success=True,
            error=None,
            pool_id=str(pool.amm_id),
            market_id=str(market.market_id),
            input_mint=str(input_mint),
            output_mint=str(output_mint),
            amount_in=amount_in,
            expected_out=expected_out,
            min_out=min_out,
            price_impact_bps=price_impact_bps,
            tx_size_bytes=len(tx_bytes),
            accounts_count=len(tx.message.account_keys),
            compute_units=compute_units,
            priority_fee_microlamports=priority_fee,
            serialized_tx_base64=base64.b64encode(tx_bytes).decode(),
            serialized_message_base64=base64.b64encode(message_bytes).decode(),
            pool_cache_hit=pool_cache_hit,
            market_cache_hit=market_cache_hit,
            pool_fetch_ms=pool_fetch_ms,
            market_fetch_ms=market_fetch_ms,
            ix_build_ms=ix_build_ms,
        )

        if self.dry_run:
            self._log_dry_run(
                result,
                reserve_in=reserve_in,
                reserve_out=reserve_out,
                source_vault=source_vault,
                dest_vault=dest_vault,
            )

        return result

    async def simulate_swap(self, dry_run_result: RaydiumDryRunResult) -> Optional[dict]:
        """
        Run transaction through simulateTransaction RPC without landing it.
        """
        try:
            tx_bytes = base64.b64decode(dry_run_result.serialized_tx_base64)
        except Exception:
            return None

        try:
            resp = self.rpc_client.simulate_transaction(
                tx_bytes, sig_verify=False, commitment="processed"
            )
        except Exception:
            return None

        value = getattr(resp, "value", None) if resp is not None else None
        if value is None:
            value = resp.get("result") if isinstance(resp, dict) else None

        if value is None:
            return None

        err = getattr(value, "err", None) if not isinstance(value, dict) else value.get("err")
        logs = getattr(value, "logs", None) if not isinstance(value, dict) else value.get("logs")
        units_consumed = (
            getattr(value, "units_consumed", None)
            if not isinstance(value, dict)
            else value.get("units_consumed")
        )

        return {
            "success": err is None,
            "error": str(err) if err else None,
            "logs": logs,
            "units_consumed": units_consumed,
        }

    def _get_pool(self, token_mint: Pubkey):
        cache_key = f"pool:{str(token_mint)}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached, True
        pool = fetch_pool_for_mint(self.rpc_client, str(token_mint))
        if pool:
            self.cache.set(cache_key, pool, hot=True)
        return pool, False

    def _get_pool_for_pair(self, mint_a: Pubkey, mint_b: Pubkey):
        """
        Attempt to locate a pool that contains both mints.
        """
        for mint in (mint_a, mint_b):
            pool, cache_hit = self._get_pool(mint)
            if pool and {pool.base_mint, pool.quote_mint} == {mint_a, mint_b}:
                return pool, cache_hit
        return None, False

    def _get_market(self, market_id: Pubkey) -> tuple[Optional[OpenBookMarketState], bool]:
        cache_key = f"market:{str(market_id)}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached, True
        try:
            resp = self.rpc_client.get_account_info(market_id, encoding="base64")
            if resp and getattr(resp, "value", None):
                data_b64 = resp.value.data[0]
            elif isinstance(resp, dict):
                data_b64 = resp.get("result", {}).get("value", {}).get("data", [None])[0]
            else:
                data_b64 = None
            if not data_b64:
                return None, False
            market = parse_market_account(
                data_base64=data_b64,
                market_pubkey=market_id,
                program_id=Pubkey.from_string(
                    os.getenv("OPENBOOK_PROGRAM_ID", "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX")
                ),
            )
            if market:
                self.cache.set(cache_key, market, hot=False)
            return market, False
        except Exception:
            return None, False

    def _fetch_vault_balances(self, pool) -> Optional[tuple[int, int]]:
        try:
            base_resp = self.rpc_client.get_token_account_balance(pool.base_vault)
            quote_resp = self.rpc_client.get_token_account_balance(pool.quote_vault)
            base_amount = int(base_resp.value.amount) if hasattr(base_resp, "value") else int(base_resp["result"]["value"]["amount"])
            quote_amount = int(quote_resp.value.amount) if hasattr(quote_resp, "value") else int(quote_resp["result"]["value"]["amount"])
            pool.base_reserve = base_amount
            pool.quote_reserve = quote_amount
            return base_amount, quote_amount
        except Exception:
            return None

    def _resolve_user_atas(self, wallet: Pubkey, input_mint: Pubkey, output_mint: Pubkey):
        owner = PublicKey(str(wallet))
        source_ata = get_associated_token_address(owner, PublicKey(str(input_mint)))
        dest_ata = get_associated_token_address(owner, PublicKey(str(output_mint)))
        return Pubkey.from_string(str(source_ata)), Pubkey.from_string(str(dest_ata))

    def _log_dry_run(
        self,
        result: RaydiumDryRunResult,
        reserve_in: Optional[int] = None,
        reserve_out: Optional[int] = None,
        source_vault: Optional[Pubkey] = None,
        dest_vault: Optional[Pubkey] = None,
    ):
        extra = ""
        if reserve_in is not None and reserve_out is not None:
            extra += f" reserves_in/out={reserve_in}/{reserve_out}"
        if source_vault and dest_vault:
            extra += f" vaults src={source_vault} dst={dest_vault}"

        print(
            f"[Raydium] Dry-run pool={result.pool_id} market={result.market_id} "
            f"in={result.amount_in} expected_out={result.expected_out} min_out={result.min_out} "
            f"impact={result.price_impact_bps/100:.2f}% tx_size={result.tx_size_bytes} "
            f"accounts={result.accounts_count} compute_units={result.compute_units}{extra}"
        )
