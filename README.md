# On-Chain Wallet Analysis

A small Python tool that fetches a wallet's token balances from the
[GoldRush API](https://goldrush.dev) and separates **trustworthy** value from
**unreliable** value.

## The problem

Price feeds will happily assign a dollar value to almost any token, including
airdropped junk and tokens traded in tiny, illiquid pools. Summing those values
gives a portfolio total that looks real but isn't.

## What I found

I ran the tool on a well-known public Ethereum wallet
(`0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045`):

| Step | Holdings | Total reported |
|---|---|---|
| Unfiltered API response | 9,054 | $573,297.63 |
| After GoldRush `no-spam=true` | 3,513 | $504,414.70 |

- The spam filter is honored by the API, and it removed 5,541 flagged tokens.
- **Three tokens that passed the filter (WHITE, SNOWMOON, MOODENG) account for
  roughly $468k of the remaining total (about 93%).** None is flagged as spam, but
  their prices come from thin markets and could not realistically be realized.
- Duplicate tickers (e.g. several "ETH", "DAI", "FOLD") are different contracts,
  so tokens must be identified by contract address, never by symbol.

*Snapshot taken November 2026. Prices and balances change; re-run for current numbers.*

> TODO: replace this section with results from the final tool run
> (verified total, unverified share) once the allowlist is checked.

## How it works

1. Fetch balances (`balances_v2`) with `no-spam=true`.
2. Convert raw integer balances: `amount = balance / 10^decimals`.
3. Classify each holding:
   - **Verified**: native gas token, or contract address on an allowlist
   - **Unverified**: everything else (value reported, not trusted)
4. Print totals, concentration warnings, and optionally export to CSV.

## Usage

```bash
pip install -r requirements.txt

# Windows (cmd)
set GOLDRUSH_API_KEY=your_key_here
# macOS / Linux
export GOLDRUSH_API_KEY=your_key_here

python goldrush_portfolio.py --wallet 0xYourWalletAddress --csv holdings.csv
```

Your API key is read from an environment variable and is never stored in the code.

## Limitations

- The allowlist is small and covers Ethereum mainnet only.
- A token being off the allowlist does not mean it is fake; it means its value
  is unverified.
- Prices come from a third-party feed and can be wrong or manipulated.
- This is an analysis tool, not financial advice.

## Roadmap

- [ ] Liquidity checks for unverified tokens
- [ ] Transaction history analysis
- [ ] Equivalent analysis in SQL on Dune Analytics
- [ ] Multi-chain support

## License

MIT
