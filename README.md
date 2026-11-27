\# On-Chain Wallet Analysis



A small Python tool that fetches a wallet's token balances from the

\[GoldRush API](https://goldrush.dev) and separates \*\*trustworthy\*\* value from

\*\*unreliable\*\* value.



\## The problem



Price feeds will happily assign a dollar value to almost any token, including

airdropped junk and tokens traded in tiny, illiquid pools. Summing those values

gives a portfolio total that looks real but isn't.



\## What I found



I ran the tool on a well-known public Ethereum wallet

(`0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045`), November 2026 snapshot:



| Metric | Value |

|---|---|

| Holdings returned (`no-spam=true`) | 3,514 |

| Verified total | $17,863.00 |

| Unverified total | $494,743.06 |

| Raw total (what a naive sum shows) | $512,606.06 |

| Unverified share of raw total | 96.5% |

| Holdings with a balance but no price | 2,479 |



\- \*\*Spam filtering works but is not enough.\*\* An unfiltered request returned

&#x20; 9,054 holdings ($573,297.63); `no-spam=true` removed 5,541 flagged tokens.

\- \*\*Three tokens that passed the filter (WHITE, SNOWMOON, MOODENG) make up about

&#x20; $476k, roughly 93% of the raw total.\*\* None is flagged as spam, but their

&#x20; prices come from thin markets and could not realistically be realized.

&#x20; WHITE alone is 66% of the unverified value.

\- \*\*Prices of these tokens are unstable.\*\* Between two runs, SNOWMOON moved from

&#x20; about $80.9k to $88.5k while ETH barely moved.

\- \*\*Symbols are not identities.\*\* Several tickers (ETH, DAI, FOLD...) appear

&#x20; multiple times as different contracts, so tokens are matched by contract

&#x20; address, never by symbol.

\- \*\*The native gas token needs special handling.\*\* GoldRush identifies it by a

&#x20; placeholder address, and my first version misclassified the wallet's ETH as

&#x20; unverified until I tested against real data.



Prices and balances change; re-run the tool for current numbers.



\## Stage 2: can the value actually be realized?



A price multiplied by a balance is not a sale price. For the top 10 unverified

holdings I looked up each token's DEX pools and capped the sellable value at

half the pool liquidity (only the quote side of a pool can be drawn out).

A later snapshot of the same wallet:



| Token | Feed value | Pool liquidity | Max proceeds (ceiling) | Status |

|---|---|---|---|---|

| WHITE | $327,430 | $2,255 | $1,127 | ILLIQUID |

| SNOWMOON | $85,570 | $130,217 | $65,109 | ILLIQUID |

| MOODENG | $57,694 | $307,254 | $57,694 | THIN |

| FOLD | $7,679 | $1,329,982 | $7,679 | OK |

| OMG | $4,977 | $3,799 | $1,900 | ILLIQUID |



(Top 10 checked in total; 5 shown. FOXE had no pool found on DEX Screener.)



\- \*\*Across the 10 checked tokens, feed value was $487,618 but the ceiling on

&#x20; proceeds was $137,078, about 72% lower.\*\*

\- \*\*WHITE's $327k feed value sits on a pool holding about $2.3k.\*\* The price

&#x20; may be correct for one token and meaningless for ten billion.

\- The ceiling ignores slippage (the slippage model below estimates it), so real proceeds would be lower still,

&#x20; especially for SNOWMOON and MOODENG, where the position is large relative

&#x20; to the pool.

\- "NO\_POOL" means DEX Screener found no pair, not that no market exists.



\## Stage 3: what would a sale actually fetch?



The "max proceeds" ceiling says what a pool could pay at most. A real sale pushes

the price down as it executes (slippage), so `--liquidity` now also estimates the

proceeds of selling the whole position.



\*\*Method\*\* (in \[`slippage.py`](slippage.py)):



\- Each pool is treated as constant-product (x \* y = k). Its two sides are worth the

&#x20; same in USD, so the reserves follow from the pool's liquidity and price.

\- A trading fee (default 0.3%, change it with `--fee-pct`) is taken from the sale.

\- The sale is split into small chunks, each routed to the pool that pays the most at

&#x20; that moment, so several pools share it.

\- Pools labelled v3, v4 or concentrated-liquidity are excluded, because their real

&#x20; depth near the current price differs from their total liquidity. The tool reports

&#x20; how much of a token's pool liquidity the model covers, and marks an estimate with

&#x20; `\*` when that is under half.

\- Not modelled: price moves during the sale, MEV, exchange or bridge liquidity,

&#x20; multi-hop routes. It is an estimate, not a quote.



The model is checked against the toy pool in the field guide (1,000 tokens and

$1,000 returns $90.91, $500.00 and $909.09 for sales of 100, 1,000 and 10,000

tokens) in `tests/test\_slippage.py`:



```bash

python -m unittest discover -s tests

```



\### Results



A later snapshot of the same wallet (late November 2026), top 10 unverified

holdings checked. The wallet and its prices move constantly, so these numbers differ

from the snapshots above.



| Token | On paper | Old ceiling | Slippage estimate | Price lost to the sale |

|---|---|---|---|---|

| WHITE | $306,891 | $1,126 | $141 \* | 100% |

| SNOWMOON | $161,213 | $84,756 | $53,545 | 63% |

| LIT | $69,095 | $69,095 | $52,853 \* | 27% |

| MOODENG | $59,513 | $59,513 | $42,676 | 29% |

| FOLD | $7,035 | $7,035 | n/a | n/a |

| OMG | $4,679 | $1,863 | $1,340 | 72% |

| ZC | $1,290 | $1,290 | n/a | n/a |

| SPDG | $862 | $862 | $850 | 4% |

| TORN | $854 | $854 | $867 \* | 2% |

| FOXE | $657 | $0 | n/a (no pool found) | n/a |



\- \*\*Across all ten,\*\* the price feed showed $612,088; the old ceiling said the pools

&#x20; could pay at most $226,393.

\- \*\*For the seven tokens the model could read,\*\* the ceiling was $218,068 and the

&#x20; estimate $152,272, about 30% lower again. Their paper value was $603,106, so the

&#x20; estimate is roughly a quarter of it.

\- \*\*SNOWMOON:\*\* the estimate is about a third of its paper value.

\- \*\*MOODENG:\*\* the old ceiling showed the full paper value, but the model loses about

&#x20; 29% of the price to the sale.

\- \*\*A star (\*) means the model read less than half of that token's pool liquidity.\*\*

&#x20; Treat starred estimates as unreliable. WHITE's ceiling alone is under 0.4% of its

&#x20; paper value, so its conclusion does not depend on the model.

\- \*\*n/a\*\* means no pool the model could read, which is not the same as unsellable.

\- The three largest unverified holdings (WHITE, SNOWMOON, LIT) were 87% of unverified

&#x20; value, which triggers the top-three concentration warning.



\## Web version



A browser-based version of the analyzer lives in \[`web/`](web/). It shows the

same verified vs. unverified split and liquidity check in a single page.

See \[web/README.md](web/README.md) for setup.



!\[Web version screenshot](web/screenshot.png)



\## How it works



1\. Fetch balances (`balances\_v2`) with `no-spam=true`.

2\. Convert raw integer balances: `amount = balance / 10^decimals`.

3\. Classify each holding:

&#x20;  - \*\*Verified\*\*: native gas token, or contract address on an allowlist

&#x20;  - \*\*Unverified\*\*: everything else (value reported, not trusted)

4\. Print totals, concentration warnings, and optionally export to CSV.

5\. With `--liquidity`: look up the top unverified holdings on DEX Screener and

&#x20;  compare each holding's feed value with what its trading pools could absorb.



\## Usage



```bash

pip install -r requirements.txt



\# Windows (cmd)

set GOLDRUSH\_API\_KEY=your\_key\_here

\# macOS / Linux

export GOLDRUSH\_API\_KEY=your\_key\_here



python goldrush\_portfolio.py --wallet 0xYourWalletAddress --csv holdings.csv



\# add the liquidity check (uses DEX Screener, no extra key needed)

python goldrush\_portfolio.py --wallet 0xYourWalletAddress --liquidity --top 10

```



Your API key is read from an environment variable and is never stored in the code.



\## Limitations



\- If GoldRush returns holdings with no prices (a pricing outage), the tool stops

&#x20; with an error instead of printing $0 totals. A missing price is never treated as zero.

\- The allowlist is small and covers Ethereum mainnet only.

\- A token being off the allowlist does not mean it is fake; it means its value

&#x20; is unverified.

\- Prices come from a third-party feed and can be wrong or manipulated.

\- This is an analysis tool, not financial advice.



\## Roadmap



\- \[x] Liquidity checks for unverified tokens

\- \[x] Slippage model (estimate proceeds from pool reserves, not just a ceiling)

\- \[ ] Read concentrated-liquidity (v3/v4) pools, which the slippage model currently skips

\- \[ ] Transaction history analysis

\- \[ ] Equivalent analysis in SQL on Dune Analytics

\- \[ ] Multi-chain support



\## License



MIT

