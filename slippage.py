"""
slippage.py - estimate what selling a whole position into DEX pools would pay.

Why: a pool's liquidity only gives a CEILING on proceeds (about half the pool).
A large sale also pushes the price down as it executes (slippage), so the real
figure is lower. This module estimates it.

Model (deliberately simple, and stated so you can challenge it):
  * Each pool is constant-product (x * y = k), the classic Uniswap-v2 design.
  * A pool's two sides are worth the same in USD, so the quote side is
    liquidity.usd / 2 and the token side is that amount / priceUsd. Using only
    liquidity.usd and priceUsd keeps the model independent of other API fields.
  * A trading fee (default 0.3%) is taken from each sale and stays in the pool.
  * The sale is split into small chunks, each routed to the pool that pays the
    most at that moment, so several pools share the sale.
  * Pools labelled v3 / v4 / concentrated-liquidity are EXCLUDED: their real
    depth near the current price is not what liquidity.usd suggests. The result
    reports what share of the token's pool liquidity the model actually covers.

Not modelled: price movement while selling, MEV, bridges/CEX liquidity,
multi-hop routes. Treat the result as an estimate, not a quote.
"""

DEFAULT_FEE = 0.003
CHUNKS = 200
CONCENTRATED_LABELS = ("v3", "v4", "clmm", "dlmm", "concentrated")


def _f(x):
    """Safe float: bad or missing values become 0.0."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _liq_usd(pair):
    return _f((pair.get("liquidity") or {}).get("usd"))


def is_concentrated(pair):
    labels = pair.get("labels") or []
    return any(str(label).lower() in CONCENTRATED_LABELS for label in labels)


def pool_reserves(pair):
    """Return (token_reserve, quote_usd_reserve) for a constant-product pool, or None."""
    liq = _liq_usd(pair)
    price = _f(pair.get("priceUsd"))
    if liq <= 0 or price <= 0:
        return None
    quote_usd = liq / 2
    return quote_usd / price, quote_usd


def simulate_sale(pools, amount, fee=DEFAULT_FEE, chunks=CHUNKS):
    """
    Sell `amount` tokens into `pools`, a list of (token_reserve, quote_usd_reserve).
    Returns total USD proceeds. Does not modify the input.
    """
    state = [list(p) for p in pools]
    if not state or amount <= 0:
        return 0.0
    chunk = amount / chunks
    total = 0.0
    for _ in range(chunks):
        s_in = chunk * (1 - fee)
        best_i, best_out = 0, -1.0
        for i, (b, q) in enumerate(state):
            out = q * s_in / (b + s_in)
            if out > best_out:
                best_i, best_out = i, out
        state[best_i][0] += chunk          # tokens (fee included) enter the pool
        state[best_i][1] -= best_out       # USD leaves the pool
        total += best_out
    return total


def estimate_sale(pairs, token_addr, amount, fee=DEFAULT_FEE):
    """
    Estimate proceeds of selling `amount` tokens of `token_addr` given DEX Screener pairs.

    Returns a dict:
      est_proceeds_usd  estimated USD proceeds, or None if nothing could be modelled
      impact_pct        how far below the pool's spot price the average sale price is (0-100)
      coverage_pct      share of the token's pool liquidity that the model covers
    """
    empty = {"est_proceeds_usd": None, "impact_pct": None, "coverage_pct": None}
    token_addr = (token_addr or "").lower()
    if not amount or amount <= 0:
        return empty

    base_pairs = [p for p in pairs
                  if ((p.get("baseToken") or {}).get("address") or "").lower() == token_addr]
    total_liq = sum(_liq_usd(p) for p in base_pairs)

    modelled, modelled_liq = [], 0.0
    for p in base_pairs:
        if is_concentrated(p):
            continue
        res = pool_reserves(p)
        if res is None:
            continue
        modelled.append(res)
        modelled_liq += _liq_usd(p)

    if not modelled:
        return empty

    proceeds = simulate_sale(modelled, amount, fee)
    deepest_b, deepest_q = max(modelled, key=lambda r: r[1])
    spot = deepest_q / deepest_b                    # equals that pool's priceUsd
    reference = amount * spot
    impact = (1 - proceeds / reference) * 100 if reference > 0 else None
    coverage = modelled_liq / total_liq * 100 if total_liq > 0 else None
    return {"est_proceeds_usd": proceeds, "impact_pct": impact, "coverage_pct": coverage}
