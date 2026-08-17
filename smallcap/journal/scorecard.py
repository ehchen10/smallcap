"""Process metrics: valid at n=3, unlike returns.

Deliberately excluded unless ``--show-pnl`` is passed: realized/unrealized
P&L, win rate, best/worst pick. At three positions those measure luck, not
process. When shown, realized P&L comes from FIFO-matching trades and
unrealized from the last close via ``prices.price_history`` — never used
anywhere else in the scorecard, since the rest of it is deliberately
price-blind.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime

from smallcap.journal import store
from smallcap.journal.models import Thesis, Trade
from smallcap.predictions import score as score_mod
from smallcap.predictions import store as pred_store

COOLING_OFF_FLAG_HOURS = 1.0


@dataclass
class ScorecardReport:
    n_theses: int
    n_trades_thesis: int
    n_trades_index: int
    pct_thesis_before_trade: float | None
    median_cooling_off_hours: float | None
    cooling_off_flagged: bool
    n_add_checks: int
    n_add_agree: int
    pct_sells_rule_followed: float | None
    n_flat_theses: int
    median_holding_days: float | None
    predictions_made: int
    predictions_resolved: int
    predictions_open_overdue: int
    early_resolutions_n: int
    early_resolutions_median_days: float | None
    brier: float | None
    brier_trend: list[tuple[str, int, float]]
    show_pnl: bool
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    pnl_note: str = ""


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def compute(*, show_pnl: bool = False) -> ScorecardReport:
    theses = store.all_theses()
    trades = store.all_trades()
    thesis_by_id = {t.id: t for t in theses}

    thesis_trades = [t for t in trades if not t.is_index]
    index_trades = [t for t in trades if t.is_index]

    pct_before = _pct_thesis_before_trade(thesis_trades, thesis_by_id)
    cooling = _cooling_off_hours(thesis_trades, thesis_by_id)
    median_cooling = statistics.median(cooling) if cooling else None
    flagged = median_cooling is not None and median_cooling < COOLING_OFF_FLAG_HOURS

    n_checks, n_agree = _add_level_agreement(thesis_trades, thesis_by_id)
    pct_sells = _pct_sells_rule_followed(thesis_trades)

    n_flat, holding_days = _holding_periods(thesis_trades)
    median_holding = statistics.median(holding_days) if holding_days else None

    preds = pred_store.all_predictions()
    resolved = pred_store.resolved_predictions()
    overdue = pred_store.due()
    early = [p for p in resolved if p.days_early is not None]

    report = score_mod.score(preds)

    result = ScorecardReport(
        n_theses=len(theses),
        n_trades_thesis=len(thesis_trades),
        n_trades_index=len(index_trades),
        pct_thesis_before_trade=pct_before,
        median_cooling_off_hours=median_cooling,
        cooling_off_flagged=flagged,
        n_add_checks=n_checks,
        n_add_agree=n_agree,
        pct_sells_rule_followed=pct_sells,
        n_flat_theses=n_flat,
        median_holding_days=median_holding,
        predictions_made=len(preds),
        predictions_resolved=len(resolved),
        predictions_open_overdue=len(overdue),
        early_resolutions_n=len(early),
        early_resolutions_median_days=statistics.median([p.days_early for p in early]) if early else None,
        brier=report.brier,
        brier_trend=score_mod.brier_trend(preds),
        show_pnl=show_pnl,
    )

    if show_pnl:
        realized, unrealized, note = _pnl(trades)
        result.realized_pnl = realized
        result.unrealized_pnl = unrealized
        result.pnl_note = note

    return result


def _pct_thesis_before_trade(trades: list[Trade], thesis_by_id: dict[str, Thesis]) -> float | None:
    checked = [t for t in trades if t.thesis_id in thesis_by_id]
    if not checked:
        return None
    n_before = sum(1 for t in checked if _parse_iso(t.executed_at) >= _parse_iso(thesis_by_id[t.thesis_id].created_at))
    return n_before / len(checked)


def _cooling_off_hours(trades: list[Trade], thesis_by_id: dict[str, Thesis]) -> list[float]:
    out = []
    for t in trades:
        thesis = thesis_by_id.get(t.thesis_id)
        if thesis is None:
            continue
        delta = _parse_iso(t.executed_at) - _parse_iso(thesis.created_at)
        out.append(delta.total_seconds() / 3600)
    return out


def _add_level_agreement(trades: list[Trade], thesis_by_id: dict[str, Thesis]) -> tuple[int, int]:
    """Cross-check the self-logged ``rule_followed`` flag on buys against
    what the numbers actually say: did the fill price sit at or below the
    pre-written add_level? Only counts buys where both a numeric add_level and
    a logged rule_followed flag exist."""
    n_checks = 0
    n_agree = 0
    for t in trades:
        if t.side != "buy" or t.rule_followed is None:
            continue
        thesis = thesis_by_id.get(t.thesis_id)
        if thesis is None or thesis.add_level is None:
            continue
        n_checks += 1
        hit_level = t.price <= thesis.add_level
        if hit_level == t.rule_followed:
            n_agree += 1
    return n_checks, n_agree


def _pct_sells_rule_followed(trades: list[Trade]) -> float | None:
    sells = [t for t in trades if t.side == "sell" and t.rule_followed is not None]
    if not sells:
        return None
    return sum(1 for t in sells if t.rule_followed) / len(sells)


def _holding_periods(trades: list[Trade]) -> tuple[int, list[float]]:
    """A thesis is 'flat' when its logged buy shares equal its logged sell
    shares. Holding period is first-buy to last-sell in days. Approximate by
    design — this isn't a lot-matching engine, just a process metric."""
    by_thesis: dict[str, list[Trade]] = {}
    for t in trades:
        by_thesis.setdefault(t.thesis_id, []).append(t)

    n_flat = 0
    days: list[float] = []
    for thesis_trades in by_thesis.values():
        buys = [t for t in thesis_trades if t.side == "buy"]
        sells = [t for t in thesis_trades if t.side == "sell"]
        if not buys or not sells:
            continue
        if abs(sum(t.shares for t in buys) - sum(t.shares for t in sells)) > 1e-6:
            continue
        n_flat += 1
        first_buy = min(_parse_iso(t.executed_at) for t in buys)
        last_sell = max(_parse_iso(t.executed_at) for t in sells)
        days.append((last_sell - first_buy).total_seconds() / 86400)
    return n_flat, days


def _pnl(trades: list[Trade]) -> tuple[float, float, str]:
    from smallcap import prices

    by_ticker: dict[str, list[Trade]] = {}
    for t in sorted(trades, key=lambda t: t.executed_at):
        by_ticker.setdefault(t.ticker, []).append(t)

    realized = 0.0
    unrealized = 0.0
    notes = []
    for ticker, ticker_trades in by_ticker.items():
        lots: list[list[float]] = []  # [shares, price] open buy lots, FIFO
        for t in ticker_trades:
            if t.side == "buy":
                lots.append([t.shares, t.price])
            else:
                remaining = t.shares
                while remaining > 1e-9 and lots:
                    lot_shares, lot_price = lots[0]
                    matched = min(lot_shares, remaining)
                    realized += matched * (t.price - lot_price)
                    lots[0][0] -= matched
                    remaining -= matched
                    if lots[0][0] <= 1e-9:
                        lots.pop(0)
        total_fees = sum(t.fees for t in ticker_trades)
        realized -= total_fees
        if lots:
            try:
                last_close = float(prices.price_history(ticker, period="5d")["Close"].iloc[-1])
                unrealized += sum(shares * (last_close - price) for shares, price in lots)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{ticker}: could not price open lot ({exc})")

    return realized, unrealized, "; ".join(notes)


def format_report(r: ScorecardReport) -> str:
    lines = [f"{r.n_theses} theses, {r.n_trades_thesis} thesis-linked trades, {r.n_trades_index} index trades"]

    if r.pct_thesis_before_trade is not None:
        lines.append(f"thesis written before execution: {r.pct_thesis_before_trade:.0%}")
    if r.median_cooling_off_hours is not None:
        flag = "  ! under 1 hour — that's a finding about you, not the data" if r.cooling_off_flagged else ""
        lines.append(f"median cooling-off (thesis -> trade): {r.median_cooling_off_hours:.1f}h{flag}")

    if r.n_add_checks:
        lines.append(f"add-level self-report matches actual fill price: {r.n_add_agree}/{r.n_add_checks}")
    if r.pct_sells_rule_followed is not None:
        lines.append(f"sells logged as exit-trigger-driven (self-reported): {r.pct_sells_rule_followed:.0%}")
    if r.median_holding_days is not None:
        lines.append(f"median holding period ({r.n_flat_theses} closed theses): {r.median_holding_days:.0f} days")

    lines.append(f"predictions: {r.predictions_made} made, {r.predictions_resolved} resolved, {r.predictions_open_overdue} open past due")
    if r.early_resolutions_n:
        lines.append(f"early resolutions: {r.early_resolutions_n} (median {r.early_resolutions_median_days:.0f} days early — check these weren't calls made once the outcome was visible)")
    if r.brier is not None:
        lines.append(f"Brier score: {r.brier:.3f}")
        if len(r.brier_trend) >= 2:
            first, last = r.brier_trend[0][2], r.brier_trend[-1][2]
            trend = "improving" if last < first else ("worsening" if last > first else "flat")
            lines.append(f"Brier trend: {first:.3f} -> {last:.3f} ({trend} over {len(r.brier_trend)} resolutions)")

    if r.show_pnl:
        lines.append(f"\n--show-pnl: realized ${r.realized_pnl:,.0f}, unrealized ${r.unrealized_pnl:,.0f}")
        if r.pnl_note:
            lines.append(f"  ({r.pnl_note})")

    return "\n".join(lines)
