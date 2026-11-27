#!/usr/bin/env python3
"""
goldrush_portfolio.py - wallet portfolio analyzer using the GoldRush API.

Why this exists: raw token "values" from price feeds can be wildly misleading.
A wallet can show $500k while ~95% of that value sits in illiquid or unflagged
junk tokens. This script separates holdings into:

  VERIFIED    native gas token + tokens whose contract address is on an allowlist
  UNVERIFIED  everything else (value is reported, but NOT trusted)

Stage 2 (--liquidity): for the top unverified holdings it asks DEX Screener
(free, no key) how much liquidity their trading pools actually hold, and
computes an UPPER BOUND on what could be sold.

Usage (Windows cmd):
    set GOLDRUSH_API_KEY=your_key_here
    python goldrush_portfolio.py --wallet 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045
    python goldrush_portfolio.py --wallet 0x... --liquidity --top 10 --csv holdings.csv

Identify tokens by CONTRACT ADDRESS, never by symbol: fake "ETH", "USDT" etc. exist.
"""
import argparse
import csv
import os
import sys
import time

import requests

from slippage import DEFAULT_FEE, estimate_sale

BASE = "https://api.covalenthq.com/v1"
DEX_BASE = "https://api.dexscreener.com"

# Placeholder address GoldRush uses for a chain's native gas token (ETH on Ethereum).
NATIVE_ADDR = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

# GoldRush chain name -> DEX Screener chain id
CHAIN_TO_DEX = {
    "eth-mainnet": "ethereum",
    "base-mainnet": "base",
    "matic-mainnet": "polygon",
    "bsc-mainnet": "bsc",
    "arbitrum-mainnet": "arbitrum",
    "optimism-mainnet": "optimism",
}

# Ethereum mainnet allowlist (lowercase). VERIFY each address on etherscan.io
# before relying on it, and extend the list for your own use case.
ALLOWLIST_ETH = {
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
    "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
    "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": "WBTC",
    "0xc18360217d8f7ab5e7c516566761ea12ce7f9d72": "ENS",
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": "stETH",
    "0x514910771af9ca656af840dff83e8264ec986ca": "LINK",
    "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984": "UNI",
    "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9": "AAVE",
}

CONCENTRATION_WARN = 0.50  # warn if one holding exceeds 50% of a bucket
TOP3_WARN = 0.75           # warn if the three largest unverified holdings exceed 75%

# Liquidity status thresholds, as (feed value) / (max proceeds ceiling):
LIQ_ILLIQUID = 1.0   # feed value >= what the pools could ever pay out
LIQ_THIN = 0.10      # selling would move the price heavily


# ---------------------------------------------------------------- GoldRush
def fetch_balances(chain, wallet, api_key, currency="USD"):
    resp = requests.get(
        f"{BASE}/{chain}/address/{wallet}/balances_v2/",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"quote-currency": currency, "no-spam": "true"},
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"{body.get('error_code')}: {body.get('error_message')}")
    return body["data"]["items"]


def is_native(item):
    """True for the chain's native gas token (e.g. ETH), by flag or placeholder address."""
    addr = (item.get("contract_address") or "").lower()
    return bool(item.get("native_token") or item.get("is_native_token") or addr == NATIVE_ADDR)


def classify(item, allowlist):
    addr = (item.get("contract_address") or "").lower()
    if is_native(item) or addr in allowlist:
        return "verified"
    return "unverified"


def parse_items(items, allowlist):
    rows = []
    for it in items:
        decimals = it.get("contract_decimals") or 0
        raw = int(it.get("balance") or 0)
        quote = it.get("quote")
        rows.append({
            "symbol": it.get("contract_ticker_symbol") or "(none)",
            "name": it.get("contract_name") or "",
            "address": (it.get("contract_address") or "").lower(),
            "amount": raw / (10 ** decimals),
            "price": it.get("quote_rate"),
            "value": quote or 0.0,
            "priced": quote is not None,
            "category": classify(it, allowlist),
            "native": is_native(it),
            "type": it.get("type") or "",
            # filled in by the liquidity stage
            "liquidity_status": "not checked",
            "liquidity_usd": None,
            "max_proceeds_usd": None,
            "dex_price": None,
            "price_diff_pct": None,
            "est_proceeds_usd": None,
            "impact_pct": None,
            "model_coverage_pct": None,
        })
    return rows


def price_feed_status(rows):
    """
    Detect a pricing outage. Returns (ok, message).

    Most holdings in a normal wallet are legitimately unpriced (airdropped junk),
    so "few priced holdings" is NOT a signal. The signal is that the well-known
    assets (native token + allowlisted tokens) all lack prices, which is what an
    outage looks like.
    """
    held = [r for r in rows if r["amount"] > 0]
    if not held:
        return True, ""
    tail = (" Totals would be meaningless, so they are not shown. This is a data-feed "
            "problem, not an empty wallet. Try again later and check your GoldRush dashboard.")
    anchors = [r for r in held if r["category"] == "verified"]
    if anchors:
        if any(r["priced"] for r in anchors):
            return True, ""
        names = ", ".join(r["symbol"] for r in anchors[:4])
        return False, (f"GoldRush returned {len(rows)} holdings but no price for any of the "
                       f"well-known assets in this wallet ({names})." + tail)
    if any(r["priced"] for r in held):
        return True, ""
    return False, f"GoldRush returned {len(rows)} holdings but none has a price." + tail


# ---------------------------------------------------------- DEX Screener
def fetch_dex_pairs(dex_chain, token_addr, retries=3):
    """All DEX pairs that contain this token on one chain. No API key needed."""
    url = f"{DEX_BASE}/token-pairs/v1/{dex_chain}/{token_addr}"
    for attempt in range(retries):
        resp = requests.get(url, timeout=30)
        if resp.status_code == 429:          # rate limited: back off and retry
            time.sleep(2 * (attempt + 1))
            continue
        resp.raise_for_status()
        data = resp.json()
        # Be tolerant of both response shapes: a bare list, or {"pairs": [...]}
        return data if isinstance(data, list) else (data.get("pairs") or [])
    raise RuntimeError("DEX Screener rate limit (HTTP 429) persisted")


def _liq_usd(pair):
    return float((pair.get("liquidity") or {}).get("usd") or 0)


def analyze_liquidity(pairs, token_addr, feed_value, feed_price, amount=None, fee=DEFAULT_FEE):
    """
    Turn DEX pairs into a liquidity verdict for one token.

    Assumptions (deliberately simple and conservative):
      * Only pairs where the token is the BASE token are used, so priceUsd is the token's price.
      * A pool's liquidity.usd counts both sides; selling can only ever draw out the
        QUOTE side, roughly half. So max proceeds <= total_liquidity / 2.
      * Slippage is ignored, so real proceeds would be LOWER than this ceiling.
    """
    token_addr = token_addr.lower()
    base_pairs = [p for p in pairs
                  if ((p.get("baseToken") or {}).get("address") or "").lower() == token_addr]
    if not base_pairs:
        return {"status": "NO_POOL", "liquidity_usd": 0.0, "max_proceeds_usd": 0.0,
                "dex_price": None, "price_diff_pct": None,
                "est_proceeds_usd": None, "impact_pct": None, "model_coverage_pct": None}

    total_liq = sum(_liq_usd(p) for p in base_pairs)
    best = max(base_pairs, key=_liq_usd)
    dex_price = float(best.get("priceUsd") or 0) or None

    ceiling = total_liq / 2
    ratio = feed_value / ceiling if ceiling > 0 else float("inf")
    if ratio >= LIQ_ILLIQUID:
        status = "ILLIQUID"
    elif ratio >= LIQ_THIN:
        status = "THIN"
    else:
        status = "OK"

    diff = None
    if feed_price and dex_price:
        diff = (feed_price - dex_price) / dex_price * 100

    est = estimate_sale(pairs, token_addr, amount, fee)
    return {"status": status, "liquidity_usd": total_liq,
            "max_proceeds_usd": min(feed_value, ceiling),
            "dex_price": dex_price, "price_diff_pct": diff,
            "est_proceeds_usd": est["est_proceeds_usd"], "impact_pct": est["impact_pct"],
            "model_coverage_pct": est["coverage_pct"]}


def run_liquidity_check(targets, dex_chain, fee=DEFAULT_FEE):
    for r in targets:
        try:
            pairs = fetch_dex_pairs(dex_chain, r["address"])
            res = analyze_liquidity(pairs, r["address"], r["value"], r["price"], r["amount"], fee)
        except Exception as exc:  # network/API trouble must not kill the whole run
            print(f'  (liquidity check failed for {r["symbol"]}: {exc})')
            r["liquidity_status"] = "ERROR"
            continue
        r["liquidity_status"] = res["status"]
        r["liquidity_usd"] = res["liquidity_usd"]
        r["max_proceeds_usd"] = res["max_proceeds_usd"]
        r["dex_price"] = res["dex_price"]
        r["price_diff_pct"] = res["price_diff_pct"]
        r["est_proceeds_usd"] = res["est_proceeds_usd"]
        r["impact_pct"] = res["impact_pct"]
        r["model_coverage_pct"] = res["model_coverage_pct"]
        time.sleep(0.25)  # stay far below the ~300 requests/minute limit


# ---------------------------------------------------------------- output
def print_bucket(title, rows, limit=None):
    print(f"\n{title}")
    print("-" * len(title))
    shown = rows[:limit] if limit else rows
    for r in shown:
        value = f'${r["value"]:>14,.2f}' if r["priced"] else f'{"n/a":>15}'
        print(f'{r["symbol"][:14]:>14}  {r["amount"]:>24,.6f}  {value}')
    if limit and len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more")


def print_liquidity(targets, v_total):
    title = f"LIQUIDITY CHECK (top {len(targets)} unverified, source: DEX Screener)"
    print(f"\n{title}")
    print("-" * len(title))
    print(f'{"symbol":>14}  {"feed value":>14}  {"pool liquidity":>15}  {"max proceeds":>13}  '
          f'{"est. proceeds":>14}  {"impact":>7}  status')
    for r in targets:
        liq = "n/a" if r["liquidity_usd"] is None else f'${r["liquidity_usd"]:,.0f}'
        mp = "n/a" if r["max_proceeds_usd"] is None else f'${r["max_proceeds_usd"]:,.0f}'
        est = "n/a" if r["est_proceeds_usd"] is None else f'${r["est_proceeds_usd"]:,.0f}'
        if r["est_proceeds_usd"] is not None and (r["model_coverage_pct"] or 0) < 50:
            est += "*"
        imp = "n/a" if r["impact_pct"] is None else f'{r["impact_pct"]:.0f}%'
        print(f'{r["symbol"][:14]:>14}  ${r["value"]:>13,.2f}  {liq:>15}  {mp:>13}  {est:>14}  {imp:>7}  {r["liquidity_status"]}')

    checked = [r for r in targets if r["max_proceeds_usd"] is not None]
    feed = sum(r["value"] for r in checked)
    ceiling = sum(r["max_proceeds_usd"] for r in checked)
    print(f"\nChecked tokens: feed value ${feed:,.2f}  ->  max proceeds (upper bound) ${ceiling:,.2f}")
    print("Max proceeds assumes only the quote side of pools can be drawn and ignores slippage,")
    print("so real proceeds would be lower. NO_POOL = no DEX pair found on DEX Screener.")
    print(f"Verified total + max proceeds of checked tokens: ${v_total + ceiling:,.2f}"
          "  (unchecked tokens excluded)")
    modelled = [r for r in checked if r["est_proceeds_usd"] is not None]
    if modelled:
        est_total = sum(r["est_proceeds_usd"] for r in modelled)
        ceil_modelled = sum(r["max_proceeds_usd"] for r in modelled)
        print(f"Slippage model ({len(modelled)} of {len(checked)} checked tokens): "
              f"estimated proceeds ${est_total:,.2f} vs ceiling ${ceil_modelled:,.2f}")
        print("Model: constant-product pools, trading fee applied, sale split across pools. "
              "Concentrated-liquidity (v3/v4) pools are skipped, so treat it as approximate.")
        if any((r["model_coverage_pct"] or 0) < 50 for r in modelled):
            print("* estimate covers under half of that token's pool liquidity.")
    else:
        print("Slippage model: no constant-product pool available to model for these tokens.")


def main():
    p = argparse.ArgumentParser(description="GoldRush wallet portfolio analyzer")
    p.add_argument("--wallet", required=True)
    p.add_argument("--chain", default="eth-mainnet")
    p.add_argument("--min-value", type=float, default=1.0, help="hide holdings below this USD value")
    p.add_argument("--top", type=int, default=10, help="how many unverified holdings to list / check")
    p.add_argument("--liquidity", action="store_true", help="check pool liquidity of top unverified holdings")
    p.add_argument("--fee-pct", type=float, default=DEFAULT_FEE * 100,
                   help="trading fee used by the slippage model, in percent (default 0.3)")
    p.add_argument("--csv", help="write all classified holdings to this CSV file")
    args = p.parse_args()

    api_key = os.environ.get("GOLDRUSH_API_KEY")
    if not api_key:
        sys.exit("Set the GOLDRUSH_API_KEY environment variable first.")

    allowlist = ALLOWLIST_ETH if args.chain == "eth-mainnet" else {}
    if not allowlist:
        print(f"Note: no allowlist for {args.chain}; only the native token counts as verified.")

    rows = parse_items(fetch_balances(args.chain, args.wallet, api_key), allowlist)
    rows.sort(key=lambda r: r["value"], reverse=True)

    ok, problem = price_feed_status(rows)
    if not ok:
        print(f"ERROR: {problem}", file=sys.stderr)
        sys.exit(2)

    verified = [r for r in rows if r["category"] == "verified" and r["value"] >= args.min_value]
    unverified = [r for r in rows if r["category"] == "unverified" and r["value"] >= args.min_value]

    v_total = sum(r["value"] for r in rows if r["category"] == "verified")
    u_total = sum(r["value"] for r in rows if r["category"] == "unverified")
    grand = v_total + u_total
    unpriced = sum(1 for r in rows if not r["priced"] and r["amount"] > 0)
    native_found = any(r["native"] for r in rows)

    print(f"Wallet: {args.wallet}   Chain: {args.chain}   Holdings returned: {len(rows)}")
    print_bucket("VERIFIED holdings (trusted)", verified)
    print_bucket(f"UNVERIFIED holdings (top {args.top}, value NOT trusted)", unverified, args.top)

    if args.liquidity:
        dex_chain = CHAIN_TO_DEX.get(args.chain)
        if dex_chain is None:
            print(f"\nNo DEX Screener chain mapping for {args.chain}; skipping liquidity check.")
        else:
            targets = unverified[:args.top]
            print(f"\nChecking liquidity for {len(targets)} tokens ...")
            run_liquidity_check(targets, dex_chain, args.fee_pct / 100)
            print_liquidity(targets, v_total)

    print("\nSUMMARY")
    print("-------")
    print(f"Verified total:      ${v_total:>14,.2f}")
    print(f"Unverified total:    ${u_total:>14,.2f}  (reported by price feed, not trusted)")
    print(f"Raw total:           ${grand:>14,.2f}")
    if grand > 0:
        print(f"Unverified share of raw total: {u_total / grand:.1%}")
    print(f"Holdings with balance but no price: {unpriced}")
    print(f"Native token detected: {'yes' if native_found else 'NO - check the address column in the CSV'}")

    # Concentration warnings
    if u_total > 0 and unverified:
        top = unverified[0]
        if top["value"] / u_total > CONCENTRATION_WARN:
            print(f'WARNING: {top["symbol"]} is {top["value"] / u_total:.0%} of unverified value. '
                  "Check its liquidity before believing this price.")
    if u_total > 0 and len(unverified) > 3:
        top3 = unverified[:3]
        share3 = sum(r["value"] for r in top3) / u_total
        if share3 > TOP3_WARN:
            names = ", ".join(r["symbol"] for r in top3)
            print(f"WARNING: the three largest unverified holdings ({names}) are {share3:.0%} of "
                  "unverified value. Check their liquidity before quoting any total.")
    if grand > 0 and u_total / grand > 0.5:
        print("WARNING: most of the raw total is unverified. Do not quote the raw total.")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
