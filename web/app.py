#!/usr/bin/env python3
"""
app.py - local web UI for the GoldRush wallet portfolio analyzer.

Run:
    set GOLDRUSH_API_KEY=your_key_here      (Windows cmd)
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000

Local use only: the server binds to 127.0.0.1 and has no login. Do not expose
it to a network, or others could spend your API credits.
"""
import os
import re
import time

import requests
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

BASE = "https://api.covalenthq.com/v1"
DEX_BASE = "https://api.dexscreener.com"
NATIVE_ADDR = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

CHAIN_TO_DEX = {
    "eth-mainnet": "ethereum",
    "base-mainnet": "base",
    "matic-mainnet": "polygon",
    "bsc-mainnet": "bsc",
    "arbitrum-mainnet": "arbitrum",
    "optimism-mainnet": "optimism",
}

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

LIQ_ILLIQUID = 1.0
LIQ_THIN = 0.10
CONCENTRATION_WARN = 0.50

WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
MAX_TOP = 25  # each checked token is one DEX Screener call; keep requests short


# ------------------------------------------------------------ GoldRush
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
    addr = (item.get("contract_address") or "").lower()
    return bool(item.get("native_token") or item.get("is_native_token") or addr == NATIVE_ADDR)


def classify(item, allowlist):
    addr = (item.get("contract_address") or "").lower()
    return "verified" if (is_native(item) or addr in allowlist) else "unverified"


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
            "liquidity_status": "not checked",
            "liquidity_usd": None,
            "max_proceeds_usd": None,
            "dex_price": None,
            "price_diff_pct": None,
        })
    return rows


# -------------------------------------------------------- DEX Screener
def fetch_dex_pairs(dex_chain, token_addr, retries=3):
    url = f"{DEX_BASE}/token-pairs/v1/{dex_chain}/{token_addr}"
    for attempt in range(retries):
        resp = requests.get(url, timeout=30)
        if resp.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else (data.get("pairs") or [])
    raise RuntimeError("DEX Screener rate limit persisted")


def _liq_usd(pair):
    return float((pair.get("liquidity") or {}).get("usd") or 0)


def analyze_liquidity(pairs, token_addr, feed_value, feed_price):
    token_addr = token_addr.lower()
    base_pairs = [p for p in pairs
                  if ((p.get("baseToken") or {}).get("address") or "").lower() == token_addr]
    if not base_pairs:
        return {"status": "NO_POOL", "liquidity_usd": 0.0, "max_proceeds_usd": 0.0,
                "dex_price": None, "price_diff_pct": None}
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
    return {"status": status, "liquidity_usd": total_liq,
            "max_proceeds_usd": min(feed_value, ceiling),
            "dex_price": dex_price, "price_diff_pct": diff}


def run_liquidity_check(targets, dex_chain):
    for r in targets:
        try:
            pairs = fetch_dex_pairs(dex_chain, r["address"])
            res = analyze_liquidity(pairs, r["address"], r["value"], r["price"])
        except Exception as exc:
            r["liquidity_status"] = f"ERROR: {exc}"
            continue
        r["liquidity_status"] = res["status"]
        r["liquidity_usd"] = res["liquidity_usd"]
        r["max_proceeds_usd"] = res["max_proceeds_usd"]
        r["dex_price"] = res["dex_price"]
        r["price_diff_pct"] = res["price_diff_pct"]
        time.sleep(0.25)


# ------------------------------------------------------------ API route
@app.route("/api/check")
def api_check():
    wallet = (request.args.get("wallet") or "").strip()
    chain = (request.args.get("chain") or "eth-mainnet").strip()
    try:
        min_value = max(0.0, float(request.args.get("min_value", 1.0)))
        top = min(MAX_TOP, max(1, int(request.args.get("top", 10))))
    except ValueError:
        return jsonify({"error": "min_value and top must be numbers"}), 400

    if not WALLET_RE.match(wallet):
        return jsonify({"error": "wallet must be a 0x address with 40 hex characters"}), 400
    if chain not in CHAIN_TO_DEX:
        return jsonify({"error": f"unsupported chain: choose one of {', '.join(CHAIN_TO_DEX)}"}), 400

    api_key = os.environ.get("GOLDRUSH_API_KEY")
    if not api_key:
        return jsonify({"error": "GOLDRUSH_API_KEY environment variable not set"}), 500

    try:
        items = fetch_balances(chain, wallet, api_key)
    except Exception as exc:
        return jsonify({"error": f"GoldRush fetch failed: {exc}"}), 502

    allowlist = ALLOWLIST_ETH if chain == "eth-mainnet" else {}
    rows = parse_items(items, allowlist)
    rows.sort(key=lambda r: r["value"], reverse=True)

    verified = [r for r in rows if r["category"] == "verified" and r["value"] >= min_value]
    unverified = [r for r in rows if r["category"] == "unverified" and r["value"] >= min_value]

    v_total = sum(r["value"] for r in rows if r["category"] == "verified")
    u_total = sum(r["value"] for r in rows if r["category"] == "unverified")
    grand = v_total + u_total

    run_liquidity_check(unverified[:top], CHAIN_TO_DEX[chain])

    checked = [r for r in unverified[:top] if r["max_proceeds_usd"] is not None]
    feed_checked = sum(r["value"] for r in checked)
    ceiling = sum(r["max_proceeds_usd"] for r in checked)

    warnings = []
    if u_total > 0 and unverified:
        t = unverified[0]
        if t["value"] / u_total > CONCENTRATION_WARN:
            warnings.append(f"{t['symbol']} is {t['value']/u_total:.0%} of unverified value. Check liquidity before believing this price.")
    if grand > 0 and u_total / grand > 0.5:
        warnings.append("Most of the raw total is unverified. Do not quote the raw total.")

    return jsonify({
        "wallet": wallet,
        "chain": chain,
        "holdings_returned": len(rows),
        "verified": verified,
        "unverified": unverified[:top],
        "totals": {
            "verified": v_total,
            "unverified": u_total,
            "raw": grand,
            "unverified_share": (u_total / grand) if grand else 0,
            "checked_feed_value": feed_checked,
            "checked_max_proceeds": ceiling,
            # Upper bound, NOT a realistic estimate: ignores slippage and unchecked tokens.
            "upper_bound_total": v_total + ceiling,
        },
        "warnings": warnings,
    })


# ------------------------------------------------------------ HTML page
PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Wallet Portfolio Analyzer</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 1200px; color: #222; }
  h1 { font-size: 1.4rem; }
  .controls { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; margin-bottom: 1rem; }
  input, select, button { font-size: 1rem; padding: .4rem .6rem; }
  input[type=text] { width: 46ch; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; font-size: .9rem; }
  th, td { border-bottom: 1px solid #e5e5e5; padding: .35rem .5rem; text-align: right; }
  th:first-child, td:first-child { text-align: left; }
  tr:hover { background: #fafafa; }
  .muted { color: #777; }
  .warn { background: #fff4e5; border: 1px solid #ffb74d; padding: .6rem .8rem; border-radius: 6px; margin-bottom: 1rem; }
  .err { background: #fdecea; border: 1px solid #e57373; padding: .6rem .8rem; border-radius: 6px; margin-bottom: 1rem; }
  .totals { display: flex; gap: 2rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
  .totals div { font-size: 1.05rem; }
  .totals strong { display: block; font-size: 1.3rem; }
  .pill { display: inline-block; padding: .1rem .5rem; border-radius: 10px; font-size: .75rem; }
  .p-OK { background: #e6f4ea; color: #137333; }
  .p-THIN { background: #fff4e5; color: #b26a00; }
  .p-ILLIQUID, .p-NO_POOL { background: #fdecea; color: #b3261e; }
  .p-not-checked { background: #eee; color: #555; }
  .p-ERROR { background: #f3e5f5; color: #6a1b9a; }
</style>
</head>
<body>
<h1>Wallet Portfolio Analyzer</h1>
<p class="muted">Paper value vs. verified value. Unverified holdings are checked against DEX Screener liquidity.</p>

<div class="controls">
  <input id="wallet" type="text" placeholder="0x wallet address"
         value="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045">
  <select id="chain">
    <option value="eth-mainnet">eth-mainnet</option>
    <option value="base-mainnet">base-mainnet</option>
    <option value="matic-mainnet">matic-mainnet</option>
    <option value="bsc-mainnet">bsc-mainnet</option>
    <option value="arbitrum-mainnet">arbitrum-mainnet</option>
    <option value="optimism-mainnet">optimism-mainnet</option>
  </select>
  <label>min $ <input id="min_value" type="number" value="1" style="width:6ch"></label>
  <label>top <input id="top" type="number" value="10" min="1" max="25" style="width:4ch"></label>
  <button id="go">Analyze</button>
</div>

<div id="status" class="muted"></div>
<div id="error"></div>
<div id="warnings"></div>
<div id="totals" class="totals"></div>
<p class="muted" style="margin-top:-1rem;font-size:.85rem">
  Upper-bound total = verified holdings + max proceeds of the checked unverified tokens.
  It is a <em>ceiling</em>, not an estimate: it ignores slippage and the unchecked long tail,
  so real proceeds would be lower. It is still far closer to reality than the raw total.
</p>
<h2 style="font-size:1.1rem">Verified holdings</h2>
<table id="verified-table"></table>
<h2 style="font-size:1.1rem">Unverified holdings (value NOT trusted)</h2>
<table id="unverified-table"></table>

<script>
const $ = id => document.getElementById(id);

// Token names/symbols are chosen by token creators and can contain HTML.
// ALWAYS escape anything that came from the data before putting it in innerHTML.
function esc(v) {
  return String(v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function fmtUsd(v) {
  if (v === null || v === undefined) return "n/a";
  return "$" + Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
function fmtAmt(v) {
  return Number(v).toLocaleString(undefined, {maximumFractionDigits: 6});
}
function pill(status) {
  const s = status || "not checked";
  const key = s.startsWith("ERROR") ? "ERROR" : s.replace(" ", "-");
  return `<span class="pill p-${key}">${esc(s)}</span>`;
}

function renderTable(el, rows, withLiquidity) {
  if (!rows.length) { el.innerHTML = "<tr><td class='muted'>No holdings above threshold.</td></tr>"; return; }
  let html = "<tr><th>Symbol</th><th>Amount</th><th>Value</th>";
  if (withLiquidity) html += "<th>Liquidity</th><th>Max proceeds</th><th>Price diff</th>";
  html += "</tr>";
  for (const r of rows) {
    html += `<tr>
      <td>${esc(r.symbol)}</td>
      <td>${fmtAmt(r.amount)}</td>
      <td>${fmtUsd(r.value)}</td>`;
    if (withLiquidity) {
      html += `<td>${pill(r.liquidity_status)}</td>
               <td>${fmtUsd(r.max_proceeds_usd)}</td>
               <td>${r.price_diff_pct === null ? "n/a" : r.price_diff_pct.toFixed(2) + "%"}</td>`;
    }
    html += "</tr>";
  }
  el.innerHTML = html;
}

async function analyze() {
  $("error").innerHTML = "";
  $("warnings").innerHTML = "";
  $("totals").innerHTML = "";
  $("verified-table").innerHTML = "";
  $("unverified-table").innerHTML = "";
  $("status").textContent = "Fetching... (liquidity checks can take a few seconds)";
  $("go").disabled = true;

  const params = new URLSearchParams({
    wallet: $("wallet").value.trim(),
    chain: $("chain").value,
    min_value: $("min_value").value,
    top: $("top").value,
  });

  try {
    const res = await fetch("/api/check?" + params);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));

    $("status").textContent = `Holdings returned: ${data.holdings_returned}`;

    if (data.warnings.length) {
      $("warnings").innerHTML = data.warnings
        .map(w => `<div class="warn">&#9888; ${esc(w)}</div>`).join("");
    }

    const t = data.totals;
    $("totals").innerHTML = `
      <div>Verified total<strong>${fmtUsd(t.verified)}</strong></div>
      <div>Upper-bound total<strong>${fmtUsd(t.upper_bound_total)}</strong></div>
      <div>Unverified (paper)<strong>${fmtUsd(t.unverified)}</strong></div>
      <div>Raw total<strong>${fmtUsd(t.raw)}</strong></div>
      <div>Unverified share<strong>${(t.unverified_share*100).toFixed(1)}%</strong></div>
      <div>Max proceeds (checked)<strong>${fmtUsd(t.checked_max_proceeds)}</strong></div>`;

    renderTable($("verified-table"), data.verified, false);
    renderTable($("unverified-table"), data.unverified, true);
  } catch (e) {
    $("error").innerHTML = `<div class="err">${esc(e.message)}</div>`;
    $("status").textContent = "";
  } finally {
    $("go").disabled = false;
  }
}

$("go").addEventListener("click", analyze);
$("wallet").addEventListener("keydown", e => { if (e.key === "Enter") analyze(); });
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    print("Open http://127.0.0.1:5000 in your browser.")
    # debug mode lets anyone who reaches the server run code; opt in explicitly.
    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
