# smallcap — quick usage guide

Five command groups: `runway`, `dilution`, `screen`, `predict`, `journal`. All read free SEC/yfinance data — no API keys.

## 1. Research a ticker

```
smallcap runway TICKER                    # cash / trailing burn, in quarters
smallcap dilution TICKER [--years 5]      # split-adjusted share growth vs. revenue growth + chart
smallcap dilution TICKER --no-chart       # skip the PNG
```
Add `--refresh` to bypass the SEC cache on either.

## 2. Screen a watchlist

```
smallcap screen --universe tickers.txt --max-runway 2 --max-dilution 0.15
```
`tickers.txt` = one ticker per line, `#` comments allowed. Idea generator only, not a buy signal.

## 3. Write a thesis (before you trade)

```
smallcap journal thesis new TICKER
```
Prompts for anything you don't pass as a flag: `claim`, `--variant-view` (why the market's wrong — required), `--falsifier` (what would prove you wrong — required), `--source` (`own-use`/`family-industry`/`social`/`screener`/`other`), `--add-level`, `--exit-trigger`. Once written, it can't be edited — only superseded:

```
smallcap journal thesis revise THESIS_ID --claim "new claim"   # new record, old one kept, status -> superseded
smallcap journal thesis close THESIS_ID --reason "played out"
smallcap journal thesis list [--status active|closed|superseded]
smallcap journal thesis show TICKER      # thesis + every trade/prediction/note, chronological
```

## 4. Log predictions and trades against it

```
smallcap predict add --ticker TICKER --claim "..." --prob 0.7 --by 2027-01-01 \
    --source "10-K filing" --thesis-id THESIS_ID

smallcap journal trade log --ticker TICKER --side buy --shares 100 --price 1.23 \
    --executed-at 2026-08-11T14:00:00 --thesis-id THESIS_ID --rule-followed yes
```
Use `--index` instead of `--thesis-id` for a trade with no thesis (e.g. an index buy).

Resolve predictions as they come due:
```
smallcap predict due                      # what's overdue
smallcap predict resolve PREDICTION_ID yes|no|void
```

## 5. Review, don't just re-decide

```
smallcap journal review [--ticker TICKER] [--refresh]
```
Rereads each active thesis, runs computed alerts (share count up >15% YoY, runway under 4 quarters), asks whether the falsifier now looks met. **Shows no price** — the point is to judge the thesis, not the stock chart.

## 6. Score yourself

```
smallcap predict score                    # overall Brier score + reliability chart (n>=20)
smallcap journal calibration --by-source  # same, split by where the idea came from
smallcap journal scorecard [--show-pnl]   # process metrics: thesis-before-trade %, cooling-off time,
                                           # add/exit-rule adherence, holding period, Brier trend
```
`--show-pnl` adds realized/unrealized P&L — off by default so you don't grade yourself on n=3 luck.

## Log a note anytime

```
smallcap journal followup add THESIS_ID --body "..."
```

## Suggested order for a first pass

1. `dilution` + `runway` on a candidate
2. `journal thesis new` — write the thesis *before* buying
3. `journal trade log`
4. `predict add` for anything with a checkable deadline
5. `journal review` monthly/quarterly
6. `journal scorecard` after a quarter of data — not before
