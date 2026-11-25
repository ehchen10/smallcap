"""Typer CLI: smallcap runway | dilution | quality | screen | predict | journal | filing | insider."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from smallcap import charts, dilution, quality, runway, screener, watch as watch_mod
from smallcap.charts import dilution_chart
from smallcap.journal import alerts as alerts_mod
from smallcap.journal import scorecard as scorecard_mod
from smallcap.journal import store as journal_store
from smallcap.journal.models import SOURCES
from smallcap.predictions import score as score_mod
from smallcap.predictions import store
from smallcap.sec import client as sec_client
from smallcap.sec import filings as filings_mod
from smallcap.sec import insiders as insiders_mod

app = typer.Typer(add_completion=False, help="Free-data small-cap research toolkit.")
predict_app = typer.Typer(add_completion=False, help="Calibration-scored prediction log.")
app.add_typer(predict_app, name="predict")
journal_app = typer.Typer(add_completion=False, help="Decision journal: thesis / trade / calibration.")
app.add_typer(journal_app, name="journal")
thesis_app = typer.Typer(add_completion=False, help="Append-only investment theses.")
journal_app.add_typer(thesis_app, name="thesis")
followup_app = typer.Typer(add_completion=False, help="Follow-up notes on a thesis.")
journal_app.add_typer(followup_app, name="followup")
note_app = typer.Typer(add_completion=False, help="General strategy notes, not tied to a ticker.")
journal_app.add_typer(note_app, name="note")
trade_app = typer.Typer(add_completion=False, help="Trade log.")
journal_app.add_typer(trade_app, name="trade")
filing_app = typer.Typer(add_completion=False, help="10-K/10-Q filing text.")
app.add_typer(filing_app, name="filing")
insider_app = typer.Typer(add_completion=False, help="Form 4 insider transactions.")
app.add_typer(insider_app, name="insider")
watch_app = typer.Typer(add_completion=False, help="Always-on screener daemon + digest for a steering Claude session.")
app.add_typer(watch_app, name="watch")


@app.command(name="runway")
def runway_cmd(ticker: str, refresh: bool = typer.Option(False, "--refresh", help="Bypass the SEC response cache.")):
    """Cash / trailing burn, in quarters."""
    r = runway.compute(ticker, refresh=refresh)
    typer.echo(runway.format_report(r))


@app.command(name="dilution")
def dilution_cmd(
    ticker: str,
    years: int = typer.Option(5, "--years", help="Lookback window."),
    chart: bool = typer.Option(True, help="Save a PNG chart alongside the summary."),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass the SEC response cache."),
):
    """Split-adjusted shares outstanding, ownership decay, share vs. revenue CAGR."""
    r = dilution.compute(ticker, years=years, refresh=refresh)
    typer.echo(f"{r.ticker} — dilution ({r.tag_used}, {r.years_covered:.1f}yr window)")
    typer.echo(f"  shares: {r.adjusted_shares[0]:,.0f} -> {r.adjusted_shares[-1]:,.0f} (split-adjusted)")
    typer.echo(f"  a fixed 1.00% stake at {r.dates[0]} is now worth {r.ownership_pct_of_initial[-1]/100:.2%}")
    if r.share_cagr is not None:
        typer.echo(f"  share-count CAGR: {r.share_cagr:+.1%}")
    if r.revenue_cagr is not None:
        typer.echo(f"  revenue CAGR:     {r.revenue_cagr:+.1%}")
        if r.share_cagr is not None and r.revenue_cagr < r.share_cagr:
            typer.echo("  ! shares are being issued faster than revenue is growing")
    if r.split_events:
        typer.echo(f"  splits in history: {', '.join(f'{d} ({ratio:.4g}x)' for d, ratio in r.split_events)}")

    if chart:
        import pandas as pd

        from smallcap import prices

        adj = pd.Series(r.adjusted_shares, index=pd.to_datetime(r.dates))
        raw = pd.Series(r.raw_shares, index=pd.to_datetime(r.dates))
        try:
            price_hist = prices.price_history(ticker, period=f"{max(1, r.years_covered):.0f}y")["Close"]
        except Exception:
            price_hist = None
        out = dilution_chart(r.ticker, adj, raw, [d for d, _ in r.split_events], price_hist)
        typer.echo(f"  chart: {out}")


@app.command(name="quality")
def quality_cmd(ticker: str, refresh: bool = typer.Option(False, "--refresh", help="Bypass the SEC response cache.")):
    """Cash flow vs. net income divergence, and restructuring-charge recurrence."""
    div = quality.cash_flow_vs_net_income(ticker, refresh=refresh)
    typer.echo(f"{div.ticker} — cash flow vs. net income ({div.ni_tag_used} / {div.ocf_tag_used})")
    if div.ttm_net_income is None or div.ttm_ocf is None:
        typer.echo("  fewer than 4 quarters of history for one of these — cannot compute TTM")
    else:
        typer.echo(f"  TTM net income: ${div.ttm_net_income:,.0f}   TTM operating cash flow: ${div.ttm_ocf:,.0f}")
        typer.echo(f"  ! {div.note}" if div.divergent else "  no divergence")

    r = quality.restructuring_recurrence(ticker, refresh=refresh)
    typer.echo(f"\n{r.ticker} — restructuring charges" + (f" ({r.tag_used})" if r.tag_used else ""))
    if not r.annual_charges:
        typer.echo("  none reported")
    else:
        for d, v in r.annual_charges:
            typer.echo(f"  {d[:4]}: ${v:,.0f}")
        if r.recurring:
            typer.echo(f"  ! reported {r.consecutive_years} consecutive fiscal years — check whether 'one-time' still applies")


_default_annual_periods = screener.default_annual_periods
_default_instant_period = screener.default_instant_period


@app.command()
def screen(
    universe: Path = typer.Option(..., "--universe", help="Text/CSV file, one ticker per line."),
    max_runway: float = typer.Option(None, "--max-runway", help="Filter to runway <= this many years."),
    max_dilution: float = typer.Option(None, "--max-dilution", help="Filter to YoY dilution <= this fraction (e.g. 0.15)."),
    instant_period: str = typer.Option(None, help='e.g. "CY2025Q1I"; defaults to the most recent likely-complete quarter.'),
    annual_end: str = typer.Option(None, help='e.g. "CY2025"; defaults to the most recent likely-complete year.'),
    annual_start: str = typer.Option(None, help='e.g. "CY2024"; defaults to one year before annual_end.'),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass the SEC response cache."),
):
    """Rank a ticker universe on cash runway and YoY dilution. Idea generator only."""
    tickers = [line.strip() for line in universe.read_text().splitlines() if line.strip() and not line.startswith("#")]
    default_end, default_start = _default_annual_periods()
    result = screener.screen(
        tickers,
        instant_period=instant_period or _default_instant_period(),
        annual_end_period=annual_end or default_end,
        annual_start_period=annual_start or default_start,
        max_runway_years=max_runway,
        max_dilution_pct=max_dilution,
        refresh=refresh,
    )
    typer.echo(screener.format_report(result))


@predict_app.command("add")
def predict_add(
    ticker: str = typer.Option(...),
    claim: str = typer.Option(...),
    prob: float = typer.Option(..., help="Probability the claim resolves 'yes', in [0, 1]."),
    by: str = typer.Option(..., "--by", help="Resolution deadline, YYYY-MM-DD."),
    source: str = typer.Option(..., "--source", help="What data will settle this claim."),
    tags: str = typer.Option("", help="Comma-separated tags."),
    notes: str = typer.Option(""),
    thesis_id: str = typer.Option(None, "--thesis-id", help="Link to a `journal thesis` record."),
):
    p = store.add(
        ticker, claim, prob, by, source,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        notes=notes,
        thesis_id=thesis_id,
    )
    typer.echo(f"added {p.id}: {p.ticker} — {p.claim} (p={p.prob:.0%}, by {p.resolve_by})")


@predict_app.command("resolve")
def predict_resolve(prediction_id: str, outcome: str = typer.Argument(..., help="'yes', 'no', or 'void'."), notes: str = typer.Option("")):
    p = store.resolve(prediction_id, outcome, notes=notes)
    typer.echo(f"resolved {p.id}: {p.claim} -> {p.outcome}")
    if p.days_early is not None:
        typer.echo(f"  ! resolved {p.days_early}d before resolve_by ({p.resolve_by}) — recorded; check this wasn't called once the outcome was visible")


@predict_app.command("score")
def predict_score(chart: bool = typer.Option(True, help="Save a reliability-diagram PNG if n is large enough.")):
    r = score_mod.score(store.all_predictions())
    typer.echo(score_mod.format_report(r))
    if chart and r.show_reliability_chart:
        out = charts.reliability_chart(r.bins)
        typer.echo(f"\nchart: {out}")


@predict_app.command("due")
def predict_due():
    overdue = store.due()
    if not overdue:
        typer.echo("nothing overdue")
        return
    for p in overdue:
        typer.echo(f"{p.id}  {p.ticker}  due {p.resolve_by} (p={p.prob:.0%})  {p.claim}")


@predict_app.command("list")
def predict_list(status: str = typer.Option(None, help="'open' or 'resolved'; omit for all.")):
    preds = store.all_predictions()
    if status:
        preds = [p for p in preds if p.status == status]
    for p in preds:
        outcome = f" -> {p.outcome}" if p.outcome else ""
        typer.echo(f"{p.id}  {p.ticker}  p={p.prob:.0%}  by {p.resolve_by}  [{p.status}{outcome}]  {p.claim}")


# ---------------------------------------------------------------- filings ---


@filing_app.command("list")
def filing_list(
    ticker: str,
    form: str = typer.Option("10-K,10-Q", help="Comma-separated form types, e.g. '10-K,10-Q,4'."),
    limit: int = typer.Option(10, help="Max filings to show."),
    refresh: bool = typer.Option(False, "--refresh"),
):
    cik = sec_client.cik_for_ticker(ticker, refresh=refresh)
    forms = tuple(f.strip() for f in form.split(",") if f.strip())
    for f in filings_mod.list_filings(cik, forms, limit=limit, refresh=refresh):
        typer.echo(f"{f.filing_date}  {f.form:6s}  report={f.report_date}  {f.accession_number}  {f.primary_document}")


@filing_app.command("going-concern")
def filing_going_concern(ticker: str, refresh: bool = typer.Option(False, "--refresh")):
    """Search the most recent 10-K/10-Q for going-concern / substantial-doubt language."""
    cik = sec_client.cik_for_ticker(ticker, refresh=refresh)
    r = filings_mod.going_concern_check(cik, refresh=refresh)
    if not r.checked:
        typer.echo(f"no 10-K/10-Q found for {ticker.upper()}")
        return
    typer.echo(f"{ticker.upper()} — {r.filing.form} filed {r.filing.filing_date} (report {r.filing.report_date})")
    if r.found:
        typer.echo("  ! going-concern / substantial-doubt language found:")
        typer.echo(f"  ...{r.snippet}...")
    else:
        typer.echo("  no going-concern language found")


@filing_app.command("8k")
def filing_8k(
    ticker: str,
    lookback: int = typer.Option(15, help="How many recent 8-Ks to check (each is a separate fetch, cached after)."),
    refresh: bool = typer.Option(False, "--refresh"),
):
    """Flag item types in the latest 8-K that this filer hasn't disclosed in its other recent 8-Ks."""
    cik = sec_client.cik_for_ticker(ticker, refresh=refresh)
    r = filings_mod.new_8k_item_types(cik, lookback=lookback, refresh=refresh)
    if not r.checked:
        typer.echo(f"no 8-Ks found for {ticker.upper()}")
        return
    typer.echo(f"{ticker.upper()} — 8-K filed {r.filing.filing_date}")
    if r.new_items:
        for item in sorted(r.new_items):
            typer.echo(f"  ! item {item} ({filings_mod.ITEM_8K_LABELS.get(item, 'other')}) — not disclosed in this filer's other recent 8-Ks (lookback={lookback})")
    else:
        typer.echo("  no item types new to this filer's recent history")


# --------------------------------------------------------------- insiders ---


@insider_app.command("list")
def insider_list(
    ticker: str,
    limit: int = typer.Option(10, help="Max Form 4 filings to pull transactions from."),
    open_market_only: bool = typer.Option(False, "--open-market-only", help="Only P (buy) / S (sell) codes — drops grants, exercises, tax withholding."),
    refresh: bool = typer.Option(False, "--refresh"),
):
    cik = sec_client.cik_for_ticker(ticker, refresh=refresh)
    txs = insiders_mod.recent_transactions(cik, limit=limit, open_market_only=open_market_only, refresh=refresh)
    if not txs:
        typer.echo("no transactions found")
        return
    for t in txs:
        kind = "deriv" if t.derivative else "common"
        ad = t.acquired_disposed or "?"
        price = f" @ {t.price_per_share:g}" if t.price_per_share else ""
        typer.echo(f"{t.transaction_date}  {t.transaction_code} ({ad})  {t.shares:g} {kind}{price}  {t.owner_name} [{t.owner_title or ('director' if t.is_director else '10%+' if t.is_ten_pct_owner else '')}]")


@insider_app.command("clusters")
def insider_clusters(
    ticker: str,
    limit: int = typer.Option(30, help="Max Form 4 filings to pull transactions from."),
    min_insiders: int = typer.Option(2, "--min-insiders"),
    window_days: int = typer.Option(30, "--window-days"),
    refresh: bool = typer.Option(False, "--refresh"),
):
    """Windows where multiple distinct insiders reported open-market buys close together."""
    cik = sec_client.cik_for_ticker(ticker, refresh=refresh)
    txs = insiders_mod.recent_transactions(cik, limit=limit, refresh=refresh)
    clusters = insiders_mod.cluster_buys(txs, min_insiders=min_insiders, window_days=window_days)
    if not clusters:
        typer.echo("no clusters found")
        return
    for c in clusters:
        typer.echo(f"{c.start_date} .. {c.end_date}  {len(c.owners)} insiders: {', '.join(c.owners)}")


# ---------------------------------------------------------------- journal ---


@thesis_app.command("new")
def thesis_new(
    ticker: str,
    claim: str = typer.Option(None, help="Plain English: what you believe."),
    variant_view: str = typer.Option(None, "--variant-view", help="Why the market disagrees. Required."),
    falsifier: str = typer.Option(None, help="What observable would prove this wrong. Required."),
    source: str = typer.Option(None, help=f"One of {', '.join(SOURCES)}."),
    add_level: float = typer.Option(None, "--add-level", help="Price at which you'd add. Optional."),
    add_level_reason: str = typer.Option("", "--add-level-reason"),
    exit_trigger: str = typer.Option("", "--exit-trigger", help="A business event, not a price."),
):
    """Create a new thesis. Missing fields are prompted for interactively."""
    claim = claim or typer.prompt("claim")
    variant_view = variant_view or typer.prompt("variant_view (why does the market disagree?)")
    falsifier = falsifier or typer.prompt("falsifier (what would prove this wrong?)")
    source = source or typer.prompt(f"source [{'/'.join(SOURCES)}]")
    if add_level is None:
        raw = typer.prompt("add_level (price, blank to skip)", default="", show_default=False)
        add_level = float(raw) if raw.strip() else None
    if add_level is not None and not add_level_reason:
        add_level_reason = typer.prompt("add_level_reason", default="", show_default=False)
    if not exit_trigger:
        exit_trigger = typer.prompt("exit_trigger (business event, not a price)", default="", show_default=False)

    t = journal_store.new_thesis(
        ticker, claim, variant_view, falsifier, source,
        add_level=add_level, add_level_reason=add_level_reason, exit_trigger=exit_trigger,
    )
    typer.echo(f"created thesis {t.id}: {t.ticker} — {t.claim}")


@thesis_app.command("revise")
def thesis_revise(
    thesis_id: str,
    claim: str = typer.Option(None),
    variant_view: str = typer.Option(None, "--variant-view"),
    falsifier: str = typer.Option(None),
    source: str = typer.Option(None),
    add_level: float = typer.Option(None, "--add-level"),
    add_level_reason: str = typer.Option(None, "--add-level-reason"),
    exit_trigger: str = typer.Option(None, "--exit-trigger"),
):
    """Write a new thesis superseding THESIS_ID. Unspecified fields carry over unchanged; the original record is never touched."""
    fields = {
        k: v for k, v in {
            "claim": claim, "variant_view": variant_view, "falsifier": falsifier, "source": source,
            "add_level": add_level, "add_level_reason": add_level_reason, "exit_trigger": exit_trigger,
        }.items() if v is not None
    }
    t = journal_store.revise(thesis_id, **fields)
    typer.echo(f"created thesis {t.id} (revises {thesis_id}): {t.ticker} — {t.claim}")


@thesis_app.command("close")
def thesis_close(thesis_id: str, reason: str = typer.Option("", "--reason")):
    journal_store.close(thesis_id, reason=reason)
    typer.echo(f"closed {thesis_id}")


@thesis_app.command("list")
def thesis_list(status: str = typer.Option(None, help="'active', 'closed', or 'superseded'; omit for all.")):
    theses = journal_store.all_theses()
    for t in theses:
        st = journal_store.thesis_status(t.id)
        if status and st != status:
            continue
        typer.echo(f"{t.id}  {t.ticker}  [{st}]  ({t.source})  {t.claim}")


@thesis_app.command("show")
def thesis_show(ticker: str):
    theses = journal_store.theses_for_ticker(ticker)
    if not theses:
        typer.echo(f"no theses for {ticker.upper()}")
        return

    for t in theses:
        st = journal_store.thesis_status(t.id)
        typer.echo(f"\n=== {t.id} [{st}]  {t.created_at}  source={t.source}{'  revises ' + t.parent_id if t.parent_id else ''} ===")
        typer.echo(f"claim:         {t.claim}")
        typer.echo(f"variant_view:  {t.variant_view}")
        typer.echo(f"falsifier:     {t.falsifier}")
        if t.add_level is not None:
            typer.echo(f"add_level:     {t.add_level} ({t.add_level_reason})")
        if t.exit_trigger:
            typer.echo(f"exit_trigger:  {t.exit_trigger}")

        timeline: list[tuple[str, str]] = []
        for f in journal_store.followups_for(t.id):
            timeline.append((f.created_at, f"[{f.kind}] {f.body}"))
        for tr in journal_store.trades_for(t.id):
            rule = "" if tr.rule_followed is None else (" (rule-followed)" if tr.rule_followed else " (improvised)")
            timeline.append((tr.executed_at, f"[trade] {tr.side} {tr.shares:g} @ {tr.price}{rule}"))
        for p in store.all_predictions():
            if p.thesis_id == t.id:
                outcome = f" -> {p.outcome}" if p.outcome else ""
                timeline.append((p.created, f"[prediction] {p.claim} (p={p.prob:.0%}){outcome}"))
        for ts, line in sorted(timeline, key=lambda x: x[0]):
            typer.echo(f"  {ts}  {line}")


@followup_app.command("add")
def followup_add(thesis_id: str, body: str = typer.Option(...)):
    journal_store.followup_add(thesis_id, body)
    typer.echo(f"added follow-up to {thesis_id}")


@followup_app.command("list")
def followup_list(thesis_id: str, kind: str = typer.Option(None, help="Filter to 'note', 'review', or 'alert'; omit for all.")):
    followups = journal_store.followups_for(thesis_id)
    if kind:
        followups = [f for f in followups if f.kind == kind]
    if not followups:
        typer.echo(f"no follow-ups for {thesis_id}" + (f" of kind={kind}" if kind else ""))
        return
    for f in followups:
        typer.echo(f"{f.created_at}  [{f.kind}]  {f.body}")


@note_app.command("add")
def note_add(body: str = typer.Option(...)):
    n = journal_store.note_add(body)
    typer.echo(f"added note {n.id}")


@note_app.command("list")
def note_list():
    notes = journal_store.all_notes()
    if not notes:
        typer.echo("no notes")
        return
    for n in notes:
        typer.echo(f"{n.created_at}  {n.body}")


@trade_app.command("log")
def trade_log_cmd(
    ticker: str = typer.Option(...),
    side: str = typer.Option(..., help="'buy' or 'sell'."),
    shares: float = typer.Option(...),
    price: float = typer.Option(...),
    executed_at: str = typer.Option(..., "--executed-at", help="Fill time, ISO 8601."),
    thesis_id: str = typer.Option(None, "--thesis-id", help="Required unless --index."),
    index: bool = typer.Option(False, "--index", help="Mark this as an index buy with no thesis."),
    fees: float = typer.Option(0.0),
    rule_followed: str = typer.Option(None, "--rule-followed", help="'yes' or 'no': did this match a pre-written add/exit level?"),
    note: str = typer.Option(""),
):
    rf = {"yes": True, "no": False}.get(rule_followed) if rule_followed is not None else None
    t = journal_store.trade_log(
        ticker, side, shares, price, executed_at,
        thesis_id=thesis_id, is_index=index, fees=fees, rule_followed=rf, note=note,
    )
    typer.echo(f"logged {t.id}: {t.side} {t.shares:g} {t.ticker} @ {t.price}")


@trade_app.command("list")
def trade_list(
    ticker: str = typer.Option(None, help="Filter to one ticker."),
    index_only: bool = typer.Option(False, "--index-only", help="Show only trades logged with --index (no thesis)."),
):
    trades = journal_store.all_trades()
    if ticker:
        trades = [t for t in trades if t.ticker.upper() == ticker.upper()]
    if index_only:
        trades = [t for t in trades if t.is_index]
    if not trades:
        typer.echo("no trades")
        return
    for t in trades:
        tag = "index" if t.is_index else t.thesis_id
        rule = "" if t.rule_followed is None else (" rule-followed" if t.rule_followed else " improvised")
        note = f"  {t.note}" if t.note else ""
        typer.echo(f"{t.id}  {t.executed_at}  {t.side:4s} {t.shares:g} {t.ticker} @ {t.price}  [{tag}]{rule}{note}")


@journal_app.command("review")
def journal_review(
    ticker: str = typer.Option(None, help="Limit to one ticker; omit to review all active theses."),
    refresh: bool = typer.Option(False, "--refresh"),
):
    """Reread each active thesis, run computed alerts, ask whether the falsifier now appears met. Shows no price — that's the point."""
    theses = journal_store.theses_for_ticker(ticker) if ticker else journal_store.all_theses()
    active = [t for t in theses if journal_store.thesis_status(t.id) == "active"]
    if not active:
        typer.echo("no active theses" + (f" for {ticker.upper()}" if ticker else ""))
        return

    for t in active:
        typer.echo(f"\n=== {t.id}  {t.ticker}  (written {t.created_at}) ===")
        typer.echo(f"claim:      {t.claim}")
        typer.echo(f"falsifier:  {t.falsifier}")
        if t.exit_trigger:
            typer.echo(f"exit_trigger: {t.exit_trigger}")

        for a in alerts_mod.check(t, refresh=refresh, persist=True):
            marker = "!" if a.is_new else " "
            typer.echo(f"  {marker} [{a.kind}] {a.message}")

        if typer.confirm("  does the falsifier now appear met?", default=False):
            note = typer.prompt("  note")
            journal_store.followup_add(t.id, f"falsifier appears met: {note}", kind="review")
            typer.echo("  recorded — consider `journal thesis revise` or `journal thesis close`")


@journal_app.command("calibration")
def journal_calibration(
    by_source: bool = typer.Option(False, "--by-source"),
    chart: bool = typer.Option(True, help="Save a reliability-diagram PNG if n is large enough."),
):
    preds = store.all_predictions()
    if not by_source:
        r = score_mod.score(preds)
        typer.echo(score_mod.format_report(r))
        if chart and r.show_reliability_chart:
            out = charts.reliability_chart(r.bins)
            typer.echo(f"\nchart: {out}")
        return

    theses = journal_store.all_theses()
    by_src = score_mod.score_by_source(preds, theses)
    typer.echo(score_mod.format_by_source_report(by_src))
    if chart:
        shown = {src: r.bins for src, r in by_src.items() if r.show_reliability_chart}
        if shown:
            out = charts.calibration_chart(shown, out_name="journal_calibration_by_source.png")
            typer.echo(f"\nchart: {out}")
        else:
            typer.echo(f"\n(reliability chart suppressed — no source bucket has n>={score_mod.MIN_N_FOR_RELIABILITY_CHART} yet)")


@journal_app.command("scorecard")
def journal_scorecard(show_pnl: bool = typer.Option(False, "--show-pnl", help="Include realized/unrealized P&L. Meaningless at low n; off by default.")):
    r = scorecard_mod.compute(show_pnl=show_pnl)
    typer.echo(scorecard_mod.format_report(r))


if __name__ == "__main__":
    app()


@watch_app.command("run")
def watch_run(once: bool = typer.Option(False, "--once", help="Run a single cycle and exit.")):
    """Run the watcher loop (what the systemd service runs). Idea generator only."""
    watch_mod.run_forever(once=once)


@watch_app.command("digest")
def watch_digest():
    """Print unacked flags, or NO_CHANGE. The cheap check a steering session runs first."""
    typer.echo(watch_mod.render_digest())


@watch_app.command("ack")
def watch_ack():
    """Mark the current digest as reviewed."""
    watch_mod.ack()
    typer.echo("acked")


@watch_app.command("set")
def watch_set(
    key: str,
    value: str = typer.Argument(..., help='JSON value: 1.5, null, ["tech"], ["8-K","10-Q"].'),
    reason: str = typer.Option(..., "--reason", help="Why — logged to changes.jsonl."),
):
    """Change one screen threshold (validated and audit-logged)."""
    try:
        old, new = watch_mod.set_screen(key, watch_mod.parse_value(value), reason)
    except watch_mod.ScreenError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{key}: {old} -> {new}")


@watch_app.command("pin")
def watch_pin(ticker: str):
    """Always watch this ticker's filings, regardless of screen hits."""
    cur = watch_mod.load_screens()["pinned"]
    watch_mod.set_screen("pinned", sorted({*cur, ticker.upper()}), f"pin {ticker.upper()}")
    typer.echo(f"pinned: {ticker.upper()}")


@watch_app.command("unpin")
def watch_unpin(ticker: str):
    cur = watch_mod.load_screens()["pinned"]
    watch_mod.set_screen("pinned", [t for t in cur if t != ticker.upper()], f"unpin {ticker.upper()}")
    typer.echo(f"unpinned: {ticker.upper()}")


@watch_app.command("status")
def watch_status():
    """Last cycle time, flag counts, and current screens."""
    state = watch_mod.load_state()
    counts = {k: sum(1 for r in watch_mod.load_tiers().values() if r["tier"] == k) for k in watch_mod.TIERS}
    typer.echo(f"last cycle: {state['last_cycle']}  unacked flags: {len(watch_mod.unacked_flags())}  tiers: {counts}")
    for k, v in sorted(watch_mod.load_screens().items()):
        typer.echo(f"  {k} = {v}")


@watch_app.command("dive")
def watch_dive(ticker: str):
    """Compact deep dive on one ticker (~15 lines)."""
    typer.echo(watch_mod.dive(ticker))


@watch_app.command("mark")
def watch_mark(
    ticker: str,
    tier: str = typer.Argument(..., help="candidate | active | park | drop"),
    note: str = typer.Option("", "--note", help="Verdict / why."),
):
    """Set a name's tier, snapshot its metrics, clear its pending triggers."""
    try:
        rec = watch_mod.mark_ticker(ticker, tier, note)
    except watch_mod.ScreenError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{ticker.upper()} -> {tier}  ({watch_mod._fmt_snap(rec['snapshot'])})")


@watch_app.command("review")
def watch_review():
    """Print the review (also kept current in watch/review.md): candidates, changed names, active list."""
    typer.echo(watch_mod.render_review())
