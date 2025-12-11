from dataclasses import dataclass
from typing import Optional

from raydium_direct.amm_math import calculate_swap_output, calculate_price_impact


@dataclass
class SizingParams:
    min_buy_sol: float = 0.01
    max_buy_sol: float = 2.0
    target_impact_bps: int = 100  # Target 1% impact
    max_impact_bps: int = 500  # Hard ceiling 5%
    max_liquidity_pct: float = 2.5  # Never take more than 2.5% of pool


@dataclass
class SizingResult:
    recommended_amount: int  # lamports
    expected_impact_bps: int
    pool_depth_sol: float
    pool_depth_usd: float
    liquidity_pct: float
    capped_by: str  # "target_impact" | "max_impact" | "max_sol" | "max_liq_pct" | "min_sol"


def calculate_optimal_buy_size(
    base_reserve: int,
    quote_reserve: int,
    base_decimals: int,
    quote_decimals: int,
    sol_price_usd: float,
    params: SizingParams,
) -> SizingResult:
    """
    Calculate optimal buy size based on pool depth and impact tolerance.

    For SOL/token pools where SOL is quote:
    - quote_reserve = SOL in pool
    - base_reserve = tokens in pool
    """

    pool_depth_sol = quote_reserve / (10**quote_decimals) if quote_decimals >= 0 else 0
    pool_depth_usd = pool_depth_sol * sol_price_usd

    def impact_for_amount(amount_lamports: int) -> int:
        """Returns price impact in BPS for a given input amount using x*y=k."""
        if amount_lamports <= 0 or quote_reserve == 0 or base_reserve == 0:
            return 0
        # Use existing AMM math to avoid drift
        output = calculate_swap_output(amount_lamports, quote_reserve, base_reserve)
        spot_price = base_reserve / quote_reserve if quote_reserve else 0
        exec_price = output / amount_lamports if amount_lamports else 0
        impact = 1 - (exec_price / spot_price) if spot_price else 1
        return max(0, int(impact * 10_000))

    low = int(params.min_buy_sol * 1e9)
    high = int(params.max_buy_sol * 1e9)
    target_amount = low

    while low <= high:
        mid = (low + high) // 2
        impact = impact_for_amount(mid)
        if impact <= params.target_impact_bps:
            target_amount = mid
            low = mid + 1
        else:
            high = mid - 1

    capped_by = "target_impact"
    final_amount = target_amount

    # Cap by max impact
    if impact_for_amount(final_amount) > params.max_impact_bps:
        while final_amount > int(params.min_buy_sol * 1e9):
            if impact_for_amount(final_amount) <= params.max_impact_bps:
                break
            final_amount = int(final_amount * 0.9)
        capped_by = "max_impact"

    # Cap by max SOL
    max_sol_lamports = int(params.max_buy_sol * 1e9)
    if final_amount > max_sol_lamports:
        final_amount = max_sol_lamports
        capped_by = "max_sol"

    # Cap by liquidity percentage
    max_liq_amount = int(quote_reserve * params.max_liquidity_pct / 100) if quote_reserve else 0
    if max_liq_amount and final_amount > max_liq_amount:
        final_amount = max_liq_amount
        capped_by = "max_liq_pct"

    # Ensure minimum
    min_lamports = int(params.min_buy_sol * 1e9)
    if final_amount < min_lamports:
        final_amount = min_lamports
        capped_by = "min_sol"

    expected_impact = impact_for_amount(final_amount)
    liquidity_pct = (final_amount / quote_reserve) * 100 if quote_reserve else 0

    return SizingResult(
        recommended_amount=final_amount,
        expected_impact_bps=expected_impact,
        pool_depth_sol=pool_depth_sol,
        pool_depth_usd=pool_depth_usd,
        liquidity_pct=liquidity_pct,
        capped_by=capped_by,
    )

