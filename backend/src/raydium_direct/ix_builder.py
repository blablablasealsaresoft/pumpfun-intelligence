from __future__ import annotations

import struct
import asyncio
from typing import Optional, Tuple, List

from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solders.message import Message
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from spl.token.instructions import get_associated_token_address
from spl.token.constants import ASSOCIATED_TOKEN_PROGRAM_ID
from solana.publickey import PublicKey
from solana.sysvar import SYSVAR_RENT_PUBKEY

from raydium_direct.pool_parser import RaydiumPoolState
from raydium_direct.market_parser import OpenBookMarketState

RAYDIUM_AMM_V4 = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
OPENBOOK_V1 = Pubkey.from_string("srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX")
SWAP_BASE_IN_IX = 9
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
RENT_SYSVAR = Pubkey.from_string(str(SYSVAR_RENT_PUBKEY))


def derive_amm_authority(amm_id: Pubkey) -> Pubkey:
    seeds = [bytes(amm_id)]
    authority, _ = Pubkey.find_program_address(seeds, RAYDIUM_AMM_V4)
    return authority


def get_reserve_mapping(pool_state: RaydiumPoolState, input_mint: Pubkey) -> tuple[int, int]:
    """
    Returns (reserve_in, reserve_out) correctly ordered based on swap direction.
    """
    if input_mint == pool_state.base_mint:
        return pool_state.base_reserve, pool_state.quote_reserve
    if input_mint == pool_state.quote_mint:
        return pool_state.quote_reserve, pool_state.base_reserve
    raise ValueError(f"Input mint {input_mint} not in pool {pool_state.amm_id}")


def get_vault_mapping(pool_state: RaydiumPoolState, input_mint: Pubkey) -> tuple[Pubkey, Pubkey]:
    """
    Returns (source_vault, dest_vault) for instruction account ordering.
    """
    if input_mint == pool_state.base_mint:
        return pool_state.base_vault, pool_state.quote_vault
    if input_mint == pool_state.quote_mint:
        return pool_state.quote_vault, pool_state.base_vault
    raise ValueError(f"Input mint {input_mint} not in pool {pool_state.amm_id}")


async def _get_account_info_async(rpc_client, account: Pubkey):
    """
    Helper to support both async and sync RPC clients.
    """
    if hasattr(rpc_client, "get_account_info_async"):
        return await rpc_client.get_account_info_async(account)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, rpc_client.get_account_info, account)


async def ensure_ata_ix(
    rpc_client,
    wallet: Pubkey,
    mint: Pubkey,
    payer: Pubkey,
) -> tuple[Pubkey, Optional[Instruction]]:
    """
    Check if ATA exists, return (ata_address, create_ix or None).
    Fully async to avoid nested event loop issues.
    """
    owner = PublicKey(str(wallet))
    mint_pk = PublicKey(str(mint))
    ata_pubkey = get_associated_token_address(owner, mint_pk)
    ata = Pubkey.from_string(str(ata_pubkey))

    resp = await _get_account_info_async(rpc_client, ata)
    value = getattr(resp, "value", None) if resp is not None else None
    if value is not None:
        return ata, None  # Already exists

    accounts = [
        AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
        AccountMeta(pubkey=ata, is_signer=False, is_writable=True),
        AccountMeta(pubkey=wallet, is_signer=False, is_writable=False),
        AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=TOKEN_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(pubkey=RENT_SYSVAR, is_signer=False, is_writable=False),
    ]
    create_ix = Instruction(
        program_id=Pubkey.from_string(str(ASSOCIATED_TOKEN_PROGRAM_ID)),
        accounts=accounts,
        data=bytes(),
    )
    return ata, create_ix


def build_swap_instruction(
    pool_state: RaydiumPoolState,
    market_state: OpenBookMarketState,
    user_wallet: Pubkey,
    user_source_ata: Pubkey,
    user_dest_ata: Pubkey,
    amount_in: int,
    min_amount_out: int,
) -> Instruction:
    data = struct.pack("<BQQ", SWAP_BASE_IN_IX, amount_in, min_amount_out)
    amm_authority = derive_amm_authority(pool_state.amm_id)

    accounts = [
        AccountMeta(pool_state.amm_id, is_signer=False, is_writable=True),
        AccountMeta(amm_authority, is_signer=False, is_writable=False),
        AccountMeta(pool_state.open_orders, is_signer=False, is_writable=True),
        AccountMeta(pool_state.target_orders, is_signer=False, is_writable=True),
        AccountMeta(pool_state.base_vault, is_signer=False, is_writable=True),
        AccountMeta(pool_state.quote_vault, is_signer=False, is_writable=True),
        AccountMeta(OPENBOOK_V1, is_signer=False, is_writable=False),
        AccountMeta(market_state.market_id, is_signer=False, is_writable=True),
        AccountMeta(market_state.bids, is_signer=False, is_writable=True),
        AccountMeta(market_state.asks, is_signer=False, is_writable=True),
        AccountMeta(market_state.event_queue, is_signer=False, is_writable=True),
        AccountMeta(market_state.base_vault, is_signer=False, is_writable=True),
        AccountMeta(market_state.quote_vault, is_signer=False, is_writable=True),
        AccountMeta(market_state.vault_signer, is_signer=False, is_writable=False),
        AccountMeta(user_source_ata, is_signer=False, is_writable=True),
        AccountMeta(user_dest_ata, is_signer=False, is_writable=True),
        AccountMeta(user_wallet, is_signer=True, is_writable=False),
        AccountMeta(TOKEN_PROGRAM, is_signer=False, is_writable=False),
    ]

    return Instruction(program_id=RAYDIUM_AMM_V4, data=data, accounts=accounts)


def build_swap_transaction(
    pool_state: RaydiumPoolState,
    market_state: OpenBookMarketState,
    user_wallet: Pubkey,
    user_source_ata: Pubkey,
    user_dest_ata: Pubkey,
    amount_in: int,
    min_amount_out: int,
    recent_blockhash: str,
    priority_fee_microlamports: int = 50_000,
    compute_units: int = 200_000,
    pre_instructions: Optional[List[Instruction]] = None,
    create_ata_ix: Optional[Instruction] = None,
) -> Transaction:
    instructions: List[Instruction] = [
        set_compute_unit_limit(compute_units),
        set_compute_unit_price(priority_fee_microlamports),
    ]

    if pre_instructions:
        instructions.extend(pre_instructions)

    if create_ata_ix:
        instructions.append(create_ata_ix)

    instructions.append(
        build_swap_instruction(
            pool_state=pool_state,
            market_state=market_state,
            user_wallet=user_wallet,
            user_source_ata=user_source_ata,
            user_dest_ata=user_dest_ata,
            amount_in=amount_in,
            min_amount_out=min_amount_out,
        )
    )
    message = Message.new_with_blockhash(instructions, user_wallet, recent_blockhash)
    return Transaction.new_unsigned(message)

