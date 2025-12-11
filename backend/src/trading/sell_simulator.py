from dataclasses import dataclass
from typing import List, Optional

from solders.pubkey import Pubkey

from raydium_direct.amm_math import calculate_swap_output
from raydium_direct import RaydiumPoolState


@dataclass
class SellSimResult:
    can_exit: bool
    expected_output_sol: int
    expected_impact_bps: int
    round_trip_loss_pct: float
    warnings: List[str]


async def simulate_sell(
    raydium_direct,
    token_mint: Pubkey,
    token_amount: int,
    pool_state: RaydiumPoolState,
    slippage_bps: int = 500,
) -> SellSimResult:
    """
    Simulate selling the tokens we'd receive from a buy.
    Used to pre-validate exit viability before entering position.
    """
    warnings: List[str] = []

    sol_reserve = pool_state.quote_reserve
    token_reserve = pool_state.base_reserve

    # Expected SOL output via constant product math
    expected_sol_out = calculate_swap_output(
        amount_in=token_amount,
        reserve_in=token_reserve,
        reserve_out=sol_reserve,
    )

    spot_price = token_reserve / sol_reserve if sol_reserve else 0
    exec_price = expected_sol_out / token_amount if token_amount else 0
    sell_impact_bps = (
        int((1 - (exec_price / spot_price)) * 10_000) if spot_price > 0 else 10_000
    )

    if sell_impact_bps > 2000:
        warnings.append(f"High sell impact: {sell_impact_bps} bps")

    if expected_sol_out < 1_000:  # < 0.000001 SOL
        warnings.append("Near-zero exit value")

    # Dry-run the swap to detect routing/account issues
    try:
        dry_run = await raydium_direct.dry_run_swap(
            input_mint=token_mint,
            output_mint=Pubkey.from_string("So11111111111111111111111111111111111111112"),
            amount_in=token_amount,
            slippage_bps=slippage_bps,
        )
        if not dry_run or not dry_run.success:
            warnings.append(f"Sell dry-run failed: {dry_run.error if dry_run else 'no result'}")
    except Exception as e:
        warnings.append(f"Sell simulation error: {e}")

    can_exit = (
        sell_impact_bps < 3000
        and expected_sol_out > 10_000
        and not any("failed" in w.lower() for w in warnings)
    )

    return SellSimResult(
        can_exit=can_exit,
        expected_output_sol=expected_sol_out,
        expected_impact_bps=sell_impact_bps,
        round_trip_loss_pct=0.0,  # Caller can add buy impact
        warnings=warnings,
    )

