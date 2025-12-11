from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Optional

from construct import Struct, Int64ul, Bytes
from solders.pubkey import Pubkey

# NOTE: Serum/OpenBook v1 market layout (partial). Offsets can change; we parse essentials.
# This parser is best-effort and will fallback if parse fails.

MARKET_LAYOUT = Struct(
    "account_flags" / Bytes(5),  # padding/flags
    "own_address" / Bytes(32),
    "vault_signer_nonce" / Int64ul,
    "base_mint" / Bytes(32),
    "quote_mint" / Bytes(32),
    "base_vault" / Bytes(32),
    "base_deposits_total" / Int64ul,
    "base_fees_accrued" / Int64ul,
    "quote_vault" / Bytes(32),
    "quote_deposits_total" / Int64ul,
    "quote_fees_accrued" / Int64ul,
    "quote_dust_threshold" / Int64ul,
    "request_queue" / Bytes(32),
    "event_queue" / Bytes(32),
    "bids" / Bytes(32),
    "asks" / Bytes(32),
    "base_lot_size" / Int64ul,
    "quote_lot_size" / Int64ul,
    "fee_rate_bps" / Int64ul,
    "referrer_rebates_accrued" / Int64ul,
    "padding" / Bytes(7 + 353),
)


@dataclass
class OpenBookMarketState:
    market_id: Pubkey
    bids: Pubkey
    asks: Pubkey
    event_queue: Pubkey
    base_vault: Pubkey
    quote_vault: Pubkey
    vault_signer: Pubkey
    vault_signer_nonce: int


def derive_vault_signer(market_id: Pubkey, nonce: int, program_id: Pubkey) -> Pubkey:
    # Serum/OpenBook vault signer PDA: seed = market_id + nonce (u64 LE)
    seed = market_id.to_bytes() + nonce.to_bytes(8, "little")
    return Pubkey.find_program_address([seed], program_id)[0]


def parse_market_account(data_base64: str, market_pubkey: Pubkey, program_id: Pubkey) -> Optional[OpenBookMarketState]:
    try:
        raw = base64.b64decode(data_base64)
        parsed = MARKET_LAYOUT.parse(raw)
        vault_signer_nonce = int(parsed.vault_signer_nonce)
        vault_signer = derive_vault_signer(market_pubkey, vault_signer_nonce, program_id)
        return OpenBookMarketState(
            market_id=market_pubkey,
            bids=Pubkey.from_bytes(parsed.bids),
            asks=Pubkey.from_bytes(parsed.asks),
            event_queue=Pubkey.from_bytes(parsed.event_queue),
            base_vault=Pubkey.from_bytes(parsed.base_vault),
            quote_vault=Pubkey.from_bytes(parsed.quote_vault),
            vault_signer=vault_signer,
            vault_signer_nonce=vault_signer_nonce,
        )
    except Exception:
        return None

