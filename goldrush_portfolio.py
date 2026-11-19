#!/usr/bin/env python3
"""
goldrush_portfolio.py - wallet portfolio analyzer using the GoldRush API.

Why this exists: raw token "values" from price feeds can be wildly misleading.
A wallet can show $500k while ~93% of that value sits in illiquid or unflagged
junk tokens. This script separates holdings into:

  VERIFIED    native gas token + tokens whose contract address is on an allowlist
  UNVERIFIED  everything else (value is reported, but NOT trusted)

Usage (Windows cmd):
    set GOLDRUSH_API_KEY=your_key_here
    python goldrush_portfolio.py --wallet 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045
    python goldrush_portfolio.py --wallet 0x... --csv holdings.csv --top 15

Identify tokens by CONTRACT ADDRESS, never by symbol: fake "ETH", "USDT" etc. exist.
"""
import argparse
import csv
import os
import sys

import requests

BASE = "https://api.covalenthq.com/v1"

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


def classify(item, allowlist):
    addr = (item.get("contract_address") or "").lower()
    if item.get("native_token") or addr in allowlist:
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
            "type": it.get("type") or "",
        })
    return rows


def print_bucket(title, rows, limit=None):
    print(f"\n{title}")
    print("-" * len(title))
    shown = rows[:limit] if limit else rows
    for r in shown:
        print(f'{r["symbol"][:14]:>14}  {r["amount"]:>24,.6f}  ${r["value"]:>14,.2f}')
    if limit and len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more")


def main():
    p = argparse.ArgumentParser(description="GoldRush wallet portfolio analyzer")
    p.add_argument("--wallet", required=True)
    p.add_argument("--chain", default="eth-mainnet")
    p.add_argument("--min-value", type=float, default=1.0, help="hide holdings below this USD value")
    p.add_argument("--top", type=int, default=10, help="how many unverified holdings to list")
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

    verified = [r for r in rows if r["category"] == "verified" and r["value"] >= args.min_value]
    unverified = [r for r in rows if r["category"] == "unverified" and r["value"] >= args.min_value]

    v_total = sum(r["value"] for r in rows if r["category"] == "verified")
    u_total = sum(r["value"] for r in rows if r["category"] == "unverified")
    grand = v_total + u_total
    unpriced = sum(1 for r in rows if not r["priced"] and r["amount"] > 0)

    print(f"Wallet: {args.wallet}   Chain: {args.chain}   Holdings returned: {len(rows)}")
    print_bucket("VERIFIED holdings (trusted)", verified)
    print_bucket(f"UNVERIFIED holdings (top {args.top}, value NOT trusted)", unverified, args.top)

    print("\nSUMMARY")
    print("-------")
    print(f"Verified total:      ${v_total:>14,.2f}")
    print(f"Unverified total:    ${u_total:>14,.2f}  (reported by price feed, not trusted)")
    print(f"Raw total:           ${grand:>14,.2f}")
    if grand > 0:
        print(f"Unverified share of raw total: {u_total / grand:.1%}")
    print(f"Holdings with balance but no price: {unpriced}")

    # Concentration warnings
    if u_total > 0 and unverified:
        top = unverified[0]
        if top["value"] / u_total > CONCENTRATION_WARN:
            print(f'WARNING: {top["symbol"]} is {top["value"] / u_total:.0%} of unverified value. '
                  "Check its liquidity before believing this price.")
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
