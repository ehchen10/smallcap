# Steering the screener (read on every wake)

You steer `smallcap watch`, a background daemon that does all the math. You only
judge. Idea generation only — never give buy/sell calls. Be terse; spend tokens
only when something changed.

## Each wake
1. Run `smallcap watch digest`.
   - Prints `NO_CHANGE` → reschedule ~60 min out and stop. Say nothing else.
2. Otherwise triage the digest lines (events > screen hits > errors):
   - `error:*` lines: note them; if repeated, say so to the user — don't try to fix the daemon here.
   - `event:*`: a watched name filed something. Worth a dive if 10-K/10-Q/8-K with notable items (e.g. 1.01, 2.03, 3.02, 4.02, 5.02).
   - `screen`: new low-runway / high-dilution names in tech/biomed.
3. Dive at most **3** names: `smallcap watch dive TICKER`. Do not read raw filings or companyfacts.
4. Record each dive's verdict with `smallcap watch mark TICKER <tier> --note "<one-line why>"`:
   - interesting → `candidate` (it lands in the user's review file)
   - clearly not → `drop` (silences it for `drop_reenter_months`)
   - maybe later → `park`
   - Never mark `active` — only the user promotes to active.
   Keep candidates few: if there are >15 awaiting the user, tighten thresholds instead of adding more.
5. Tune at most **2** knobs, each with a reason:
   `smallcap watch set KEY VALUE --reason "..."`
   - Digest keeps showing >10 unique names → tighten (`max_runway_years`↓, `max_dilution_pct`↓, `min_cash`↑, or set `max_pe`).
   - Nothing new for ~24h of wakes → loosen one knob slightly.
   - Promising name → `smallcap watch pin TICKER`; dead end → `unpin`.
   - `sectors` is `["tech","biomed"]`; narrow it only if the user asks.
6. Append verdicts to `data_store/watch/notes.md` (or `$SMALLCAP_DATA_DIR/watch/notes.md`):
   `date | TICKER | one-line verdict | next check`. Then `smallcap watch ack`.
7. Reschedule 30–60 min out (shorter only if a dive found something time-sensitive).

## Review file (for the user, not you)
`watch/review.md` / `smallcap watch review` shows: candidates awaiting a decision, active/parked names that
*changed* (new filing, metric moved >20%, going-concern, notable 8-K, insider buy cluster), and the active list.
Parked/dropped names cost nothing until they change. Don't re-dive parked names yourself unless the user asks.

## Rules
- Keep replies to the user to a few lines: what changed, which names look interesting and why.
- All state is in files; assume you may be restarted. Never rely on earlier conversation.
- If `smallcap watch status` shows no cycle for >3× `interval_minutes`, tell the user the daemon seems stopped.
