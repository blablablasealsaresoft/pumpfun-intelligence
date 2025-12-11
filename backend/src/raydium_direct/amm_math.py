def calculate_swap_output(amount_in: int, reserve_in: int, reserve_out: int, fee_numerator: int = 25, fee_denominator: int = 10000) -> int:
    """
    Standard x*y=k with fee deduction (Raydium ~0.25%).
    """
    amount_in_with_fee = amount_in * (fee_denominator - fee_numerator)
    numerator = amount_in_with_fee * reserve_out
    denominator = (reserve_in * fee_denominator) + amount_in_with_fee
    return numerator // denominator if denominator else 0


def calculate_swap_input(amount_out: int, reserve_in: int, reserve_out: int, fee_numerator: int = 25, fee_denominator: int = 10000) -> int:
    """
    Inverse: given desired output, calculate required input.
    """
    numerator = reserve_in * amount_out * fee_denominator
    denominator = (reserve_out - amount_out) * (fee_denominator - fee_numerator)
    return (numerator // denominator) + 1 if denominator else 0


def calculate_price_impact(amount_in: int, reserve_in: int, reserve_out: int) -> float:
    """
    Returns price impact as decimal (0.01 = 1%).
    """
    if reserve_in == 0 or reserve_out == 0 or amount_in == 0:
        return 0.0
    spot_price = reserve_out / reserve_in
    output = calculate_swap_output(amount_in, reserve_in, reserve_out)
    if output == 0:
        return 0.0
    exec_price = output / amount_in
    return 1 - (exec_price / spot_price)

