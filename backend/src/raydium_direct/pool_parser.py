from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Optional

from construct import Struct, Int64ul, Bytes, Int8ul
from solders.pubkey import Pubkey
import os

# Raydium Liquidity Pool V4 layout (partial, key fields)
# Total size ~ 752 bytes; we parse only required fields.

LIQUIDITY_POOL_V4_LAYOUT = Struct(
    "status" / Int64ul,
    "nonce" / Int64ul,
    "order_depth" / Int64ul,
    "base_mint" / Bytes(32),
    "quote_mint" / Bytes(32),
    "lp_mint" / Bytes(32),
    "base_vault" / Bytes(32),
    "quote_vault" / Bytes(32),
    "amm_authority" / Bytes(32),
    "open_orders" / Bytes(32),
    "target_orders" / Bytes(32),
    "base_decimal" / Int8ul,
    "quote_decimal" / Int8ul,
    "state_1" / Bytes(2),  # padding/flags
    "swap_base_in_amount" / Int64ul,
    "swap_quote_out_amount" / Int64ul,
    "swap_quote_in_amount" / Int64ul,
    "swap_base_out_amount" / Int64ul,
    "lp_decimal" / Int8ul,
    "padding" / Bytes(7 + 528),  # skip the rest
)


@dataclass
class RaydiumPoolState:
    amm_id: Pubkey
    base_mint: Pubkey
    quote_mint: Pubkey
    base_vault: Pubkey
    quote_vault: Pubkey
    amm_authority: Pubkey
    open_orders: Pubkey
    target_orders: Pubkey
    base_decimal: int
    quote_decimal: int
    status: int
    serum_market: Optional[Pubkey] = None
    base_reserve: int = 0
    quote_reserve: int = 0


def parse_pool_account(data_base64: str) -> Optional[RaydiumPoolState]:
    try:
        raw = base64.b64decode(data_base64)
        parsed = LIQUIDITY_POOL_V4_LAYOUT.parse(raw)
        return RaydiumPoolState(
            amm_id=Pubkey.from_bytes(bytes(32)),  # to be set by caller
            base_mint=Pubkey.from_bytes(parsed.base_mint),
            quote_mint=Pubkey.from_bytes(parsed.quote_mint),
            base_vault=Pubkey.from_bytes(parsed.base_vault),
            quote_vault=Pubkey.from_bytes(parsed.quote_vault),
            amm_authority=Pubkey.from_bytes(parsed.amm_authority),
            open_orders=Pubkey.from_bytes(parsed.open_orders),
            target_orders=Pubkey.from_bytes(parsed.target_orders),
            base_decimal=int(parsed.base_decimal),
            quote_decimal=int(parsed.quote_decimal),
            status=int(parsed.status),
        )
    except Exception:
        return None


def fetch_pool_for_mint(rpc_client, token_mint: str) -> Optional[RaydiumPoolState]:
    """
    Fetch a Raydium Liquidity V4 pool matching token_mint as base or quote.
    """
    try:
        memcmp_filters = [
            {"memcmp": {"offset": 8 + 8 + 8, "bytes": token_mint}},  # base_mint position
            {"memcmp": {"offset": 8 + 8 + 8 + 32, "bytes": token_mint}},  # quote_mint position
        ]
        for f in memcmp_filters:
            resp = rpc_client.get_program_accounts(
                Pubkey.from_string(os.getenv("RAYDIUM_AMM_PROGRAM", "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")),
                encoding="base64",
                data_slice=None,
                filters=[f],
            )
            accounts = resp.value if hasattr(resp, "value") else resp.get("result", [])
            if accounts:
                acc = accounts[0]
                data_b64 = acc.account.data[0] if hasattr(acc.account, "data") else acc["account"]["data"][0]
                pool = parse_pool_account(data_b64)
                if pool:
                    pool.amm_id = acc.pubkey if hasattr(acc, "pubkey") else Pubkey.from_string(acc.get("pubkey"))
                    # Try to read serum_market from the tail if present (best effort)
                    try:
                        raw = base64.b64decode(data_b64)
                        # serum_market often at offset ~ 360-392; best effort slice
                        serum_market_bytes = raw[360:392]
                        pool.serum_market = Pubkey.from_bytes(serum_market_bytes)
                    except Exception:
                        pass
                    return pool
    except Exception:
        return None
    return None

