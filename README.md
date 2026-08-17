# smallcap

Small-cap fundamentals toolkit on free, keyless data: SEC XBRL (`data.sec.gov`),
yfinance. No API keys.

**Not investment advice.** This reads and reformats public SEC/EDGAR data —
it makes no buy/sell recommendation, and every number can be wrong (see "Why
this exists" below for the specific ways raw XBRL data misleads). Verify
anything before acting on it.

```
pip install -e ".[dev]"
export SEC_USER_AGENT="your-project-name you@example.com"   # required — see below
```

SEC requires a real identifying User-Agent (project name + contact email) on
every request to its EDGAR/XBRL endpoints — see
[SEC's developer FAQ](https://www.sec.gov/os/webmaster-faq#developers). There
is no built-in default; commands fail with a clear error until
`SEC_USER_AGENT` is set to your own contact info.

## Why this exists

SEC XBRL facts look ready to use and aren't. Read straight off the API:

1. **Cash-flow facts are YTD-cumulative**, not per-quarter, within a fiscal
   year — summing four of them overstates annual burn by up to ~2.5x.
2. **Periods are duplicated** across the original filing, amendments, and
   restated comparative columns in later filings — same period, different
   numbers, no obvious "correct" one without picking a policy.
3. **Share counts are as-reported, not split-adjusted.** A reverse split makes
   the raw count *drop* right when a holder is being diluted — the opposite of
   the signal a dilution tracker exists to show.
4. **A historical balance-sheet date's share count can get retroactively
   restated** in a later filing's comparative column after a split, while an
   adjacent period never is — so two "latest filed" facts for neighboring
   periods can be stated in different split eras and aren't safely
   diffable without knowing which is which.

`smallcap/sec/normalize.py` and `smallcap/sec/concepts.py` fix (1)-(3) once,
centrally. (4) doesn't have a general fix — `screener.py` detects when it's at
risk and flags the number instead of reporting it. All four were found and
verified against live data for CIK 1174940 (Oragenics, ticker **OGEN**) while
building this; see docstrings in those files for the numbers.

The journal (below) is a different kind of correctness problem: not bad data,
but a system that would let *you* quietly lie to yourself. Three failure modes
it closes off by construction rather than by discipline:

5. **Rewriting a thesis after the fact.** `journal/store.py` has no update or
   delete function for a `Thesis` — there is no code path that can overwrite
   what you originally wrote. Changing your mind writes a brand-new record
   with `parent_id` set; the original line in `theses.jsonl` is never touched.
6. **Judging a prediction by what the trade did.** Predictions resolve true or
   false on their own merits (`predictions/score.py`, reused as-is), and can
   optionally link to a thesis via `thesis_id` — but nothing about scoring a
   prediction looks at P&L.
7. **Measuring returns at n=3.** `journal scorecard` reports process metrics
   (thesis-before-trade %, cooling-off time, exit-trigger adherence,
   prediction calibration) and omits P&L entirely unless you pass
   `--show-pnl` — at three positions, returns measure luck, not process.

## Commands

```
smallcap runway TICKER                  # cash / trailing burn, in quarters
smallcap dilution TICKER [--years N]    # split-adjusted shares, ownership decay, chart
smallcap quality TICKER                 # cash-flow vs. net-income divergence, restructuring recurrence
smallcap screen --universe FILE.txt     # rank a ticker list on runway + dilution
smallcap filing list/going-concern/8k TICKER    # 10-K/10-Q/8-K filing text search
smallcap insider list/clusters TICKER   # Form 4 insider transactions, open-market buy clustering
smallcap predict add/resolve/score/due  # calibration-scored prediction log
smallcap journal thesis/followup/note/trade/review/calibration/scorecard  # append-only decision journal
```

`runway` and `dilution` reconcile one company's full history and are the
precision path. `screen` uses SEC's `frames` endpoint (one request per
concept, every filer at once) to rank a universe cheaply — it's an idea
generator, never a buy signal, and is explicitly coarser (see its docstring).
`journal` sits on top of `predict`: a thesis records what you believed and why
*before* you acted, then `predict add --thesis-id` attaches falsifiable claims
to it, and `journal scorecard` grades the process.

## CLI reference

Every command also takes `--help`; `smallcap --help` / `smallcap predict --help`
list all subcommands. `--refresh` (where present) bypasses the 24h SEC
response cache.

### `smallcap runway TICKER [--refresh]`

Cash ÷ trailing operating cash burn.

```
$ smallcap runway OGEN
OGEN — runway
  cash: $6,107,078 (as of 2026-03-31, filed 2026-05-08, 94d ago; tag us-gaap:CashAndCashEquivalentsAtCarryingValue)
  TTM burn: $9,334,941/yr ($2,333,735/quarter avg; tag us-gaap:NetCashProvidedByUsedInOperatingActivities)
  runway (TTM avg burn): 2.6 quarters
  runway (worst of last 4 quarters): 2.0 quarters
  latest quarter burning slower the TTM average (0.94x)
  ! cash figure is 94 days old — runway today is shorter than shown
```

### `smallcap dilution TICKER [--years N] [--chart/--no-chart] [--refresh]`

Split-adjusted shares outstanding, ownership decay, share-count vs. revenue
CAGR. Writes `data_store/charts/{TICKER}_dilution.png` unless `--no-chart`.

```
$ smallcap dilution OGEN --years 6
OGEN — dilution (dei:EntityCommonStockSharesOutstanding, 5.5yr window)
  shares: 33,892 -> 4,511,957 (split-adjusted)
  a fixed 1.00% stake at 2020-11-06 is now worth 0.75%
  share-count CAGR: +143.4%
  revenue CAGR:     -8.9%
  ! shares are being issued faster than revenue is growing
  splits in history: 2010-09-28 (0.05x), 2018-01-22 (0.1x), 2023-01-23 (0.01667x), 2025-06-03 (0.03333x)
  chart: data_store/charts/OGEN_dilution.png
```

`--years` is a lookback window, not a hard cutoff: if fewer than 2 data points
fall inside it, the command falls back to whatever history exists.

### `smallcap screen --universe FILE.txt [options]`

```
--max-runway FLOAT     filter to runway <= this many years
--max-dilution FLOAT   filter to YoY dilution <= this fraction (e.g. 0.15)
--instant-period STR   e.g. "CY2025Q1I"; defaults to the most recent likely-complete quarter
--annual-end STR       e.g. "CY2025"; defaults to the most recent likely-complete year
--annual-start STR     e.g. "CY2024"; defaults to one year before annual_end
```

`--universe` is a text file, one ticker per line (`#` comments allowed).

```
$ smallcap screen --universe watchlist.txt --max-runway 1
screened 10 tickers, 10 matched (100% coverage) — instant=CY2026Q2I annual=CY2024->CY2025
idea generator only — never a buy signal on its own

ticker  runway(yr)  dilution%   cash            annual OCF
PLUG    0.30        -           161,894,000     -535,835,000
```

Rows marked `?split?` in the dilution column mean a split fell inside the
window and the two share-count facts may be in different restatement eras —
not safely comparable; use `smallcap dilution <ticker>` instead (see README
point 4 above).

### `smallcap quality TICKER [--refresh]`

Two accrual-quality checks, both structural — no LLM judgment call:

- **cash-flow vs. net-income divergence**: TTM net income positive but TTM
  operating cash flow negative (or covering less than half of it) — profit on
  paper, not in the bank. Filters net-income facts to actual 10-K/10-Q forms
  only; SEC's pay-vs-performance proxy rule (Item 402(v)) has DEF 14A filings
  tag a "Company Net Income" figure under the same XBRL tag as real net
  income, and it can be ~100x off (confirmed on OGEN).
- **restructuring-charge recurrence**: nonzero restructuring charges 3+
  consecutive fiscal years — calling something "one-time" three years running
  is a labeling problem.

### `smallcap filing <list|going-concern|8k> TICKER [--refresh]`

Reads actual filing document text (HTML-stripped), not just XBRL numbers —
the free EDGAR equivalent of "open the 10-K and Ctrl-F."

```
smallcap filing list TICKER [--form 10-K,10-Q] [--limit N]
smallcap filing going-concern TICKER
    # substring search for going-concern / substantial-doubt language in the
    # latest 10-K/10-Q; journal review only alerts when this newly appears,
    # not every quarter it persists
smallcap filing 8k TICKER [--lookback N]
    # flags item types (e.g. "5.02" departure of a director) in the latest
    # 8-K that this filer hasn't disclosed in its other recent 8-Ks
```

### `smallcap insider <list|clusters> TICKER [--refresh]`

Form 4 open-market transactions.

```
smallcap insider list TICKER
smallcap insider clusters TICKER [--limit N] [--window-days N] [--min-insiders N]
    # windows where multiple distinct insiders reported open-market buys
    # close together — a single buy is noise, several people buying the
    # same week is a different kind of signal
```

### `smallcap predict <add|resolve|score|due|list>`

```
smallcap predict add --ticker T --claim "..." --prob 0.7 --by 2027-01-01 \
    --source "what data will settle this" [--tags a,b] [--notes "..."] [--thesis-id ID]

smallcap predict resolve <id> <yes|no|void> [--notes "..."]
    # resolving before resolve_by records days_early and warns; not blocked

smallcap predict score [--chart/--no-chart]
    # Brier score, base rate, skill score vs. base-rate guessing,
    # over/under-confidence, reliability bins. Reliability chart suppressed
    # below n=20 resolved predictions.

smallcap predict due
    # open predictions past their resolve_by date

smallcap predict list [--status open|resolved]
```

`--source` is required at creation time — claims without a stated resolution
source are rejected. Example:

```
$ smallcap predict add --ticker OGEN \
    --claim "OGEN raises capital via ATM within 6mo" --prob 0.75 \
    --by 2027-02-01 --source "SEC EDGAR full text search for prospectus supplements"
added b6575483: OGEN — OGEN raises capital via ATM within 6mo (p=75%, by 2027-02-01)

$ smallcap predict resolve b6575483 yes
resolved b6575483: OGEN raises capital via ATM within 6mo -> yes

$ smallcap predict score
n = 1 resolved prediction
Brier score: 0.062 (lower is better; 0 = perfect, 0.25 = always guess 50%)
base rate: 100% of resolved claims were 'yes'
skill score: n/a (base rate is 0% or 100%)
mean predicted probability: 75% (underconfident, -25% vs. base rate)
```

### `smallcap journal <thesis|followup|trade|review|calibration|scorecard>`

```
smallcap journal thesis new TICKER [--claim ...] [--variant-view ...] [--falsifier ...]
    [--source own-use|family-industry|social|screener|other]
    [--add-level PRICE --add-level-reason "..."] [--exit-trigger "..."]
    # missing fields are prompted for; refuses to save with empty variant_view/falsifier

smallcap journal thesis show TICKER      # thesis + follow-ups + predictions + trades, chronological
smallcap journal thesis list [--status active|closed|superseded]
smallcap journal thesis revise THESIS_ID [--claim ...] [...]
    # writes a NEW record with parent_id set; the original is never touched
smallcap journal thesis close THESIS_ID --reason "..."

smallcap journal followup add THESIS_ID --body "..."
smallcap journal followup list THESIS_ID [--kind note|review|alert]

smallcap journal note add --body "..."     # general strategy notes, not tied to any ticker/thesis
smallcap journal note list

smallcap journal trade log --ticker T --side buy|sell --shares N --price P \
    --executed-at ISO8601 (--thesis-id ID | --index) [--fees F] \
    [--rule-followed yes|no] [--note "..."]

smallcap journal review [--ticker T] [--refresh]
    # computed alerts (dilution >15% YoY, runway <4 quarters) + falsifier reread
    # prompt. Never fetches or prints a price — the whole point of the prompt
    # is rereading the thesis without the market's opinion in front of you.

smallcap journal calibration [--by-source] [--chart]
    # --by-source buckets by the linked thesis's `source` field — if
    # social-sourced ideas score worse than own-use ones, this is where it shows

smallcap journal scorecard [--show-pnl]
    # process metrics only by default: thesis-before-trade %, cooling-off time,
    # add-level/exit-trigger adherence, prediction Brier + trend. Returns are
    # meaningless at low n and stay hidden unless --show-pnl is passed.
```

`--source` and `--variant-view`/`--falsifier` are required at creation and
never overwritable — see "Why this exists" point 5 above. Example, continuing
from the `predict add --thesis-id` example:

```
$ smallcap journal thesis new OGEN --claim "..." \
    --variant-view "Market treats OGEN as a pure serial-diluter and ignores the SmartMouth royalty base" \
    --falsifier "If TTM OCF burn worsens past -\$3M/quarter with no revenue growth, thesis is wrong" \
    --source own-use --add-level 0.50 --add-level-reason "below cash-adjusted book value" \
    --exit-trigger "SmartMouth royalty renewal falls through"
created thesis 7c48e6d2: OGEN — ...

$ smallcap journal trade log --ticker OGEN --side buy --shares 1000 --price 0.48 \
    --executed-at 2026-08-11T18:17:59+00:00 --thesis-id 7c48e6d2 --rule-followed yes
logged ae4dde5c: buy 1000 OGEN @ 0.48

$ smallcap journal review --ticker OGEN
=== 7c48e6d2  OGEN  (written 2026-08-11T18:17:51+00:00) ===
claim:      ...
falsifier:  If TTM OCF burn worsens past -$3M/quarter with no revenue growth, thesis is wrong
  ! [dilution-yoy] share count up +1008.3% over the lookback window (split-adjusted, 2024-11-13 -> 2026-05-07)
  ! [runway-low] runway is 2.6 quarters (TTM avg burn), under the 4-quarter threshold
  does the falsifier now appear met? [y/N]:

$ smallcap journal scorecard
1 theses, 1 thesis-linked trades, 0 index trades
thesis written before execution: 100%
median cooling-off (thesis -> trade): 0.0h  ! under 1 hour — that's a finding about you, not the data
add-level self-report matches actual fill price: 1/1
predictions: 2 made, 2 resolved, 0 open past due
early resolutions: 2 (median 142 days early — check these weren't calls made once the outcome was visible)
Brier score: 0.160
```

The cooling-off warning above is real output, not a contrived example — this
walkthrough was written and traded in the same test run, which is exactly the
pattern the metric exists to catch.

## Layout

- `sec/client.py` — cached (24h TTL), rate-limited fetch for companyfacts,
  companyconcept, frames, and the ticker→CIK map.
- `sec/normalize.py` — dedup, YTD de-cumulation, TTM, split-adjustment. Load-
  bearing; everything else depends on it.
- `sec/concepts.py` — tag fallback chains (companies tag the same fact under
  different XBRL concepts; small caps are worse about it than large caps).
- `dilution.py`, `runway.py`, `screener.py`, `quality.py` — the fundamentals
  projects (share dilution, cash runway, universe screening, accrual quality).
- `sec/filings.py` — filing document text (HTML-stripped): going-concern
  language search, 8-K item-type parsing.
- `sec/insiders.py` — Form 4 transaction parsing and open-market buy
  clustering.
- `predictions/` — the calibration-scored prediction log, no SEC dependency.
  `journal/` builds on it via `Prediction.thesis_id` rather than duplicating it.
- `journal/` — append-only thesis/follow-up/note/trade storage (`store.py`, no
  update or delete path for a thesis by design), computed alerts (`alerts.py`:
  dilution, runway, going-concern, cash-flow divergence, restructuring
  recurrence, insider-buy clustering, new 8-K item types — deduped so review
  doesn't re-fire the same warning every quarter), and process metrics
  (`scorecard.py`, no P&L unless asked).
- `charts.py` — matplotlib PNGs under `data_store/charts/`.

## Tests

```
pytest
```

`tests/fixtures/oragenics_companyfacts.json` is a trimmed real payload for
CIK 1174940. The normalize.py tests assert exact numbers verified against the
live SEC API — they're a regression guard on the three correctness fixes
above, not just shape checks.

`tests/test_journal.py` tests the immutability constraints directly rather
than trusting the docstrings: it asserts no `update_thesis`/`delete_thesis`
symbol exists, that no creation function's signature accepts a `created_at`,
and that `revise()` leaves the parent's raw JSONL line byte-identical.
