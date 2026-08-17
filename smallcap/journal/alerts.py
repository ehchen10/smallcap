"""Computed alerts: the "bad news you'd otherwise avoid looking at" half of
``journal review``. Seven checks, all reusing existing modules rather than
recomputing anything:

- share count up >15% YoY (dilution.py)
- runway under 12 months / 4 quarters (runway.py)
- going-concern / substantial-doubt language newly appearing in a 10-K/10-Q
  that didn't have it last time (sec/filings.py)
- TTM operating cash flow diverging from TTM net income (quality.py)
- restructuring charges reported 3+ consecutive fiscal years (quality.py)
- 2+ distinct insiders reporting open-market buys within 30 days (sec/insiders.py)
- an 8-K item type this filer has never disclosed before (sec/filings.py)

The going-concern check is a plain substring match on the filing text — it
flags that the words appear, not anything about severity, but it only fires
when the language is new versus the prior filing (``going_concern_newly_appeared``),
so the snippet is always fresh, not the same finding repeated every quarter.

Tuned to be quiet: every check here is either a structural threshold (share
dilution, runway, cash/earnings divergence, 3-year recurrence) or a
"this is new" comparison (going-concern, 8-K item types) — never "this
number changed a little." An alert system that fires constantly gets ignored;
these are meant to be rare and, when they fire, worth ten seconds of
verification against the cited filing.

Deliberately not attempted here: "falsifier now appears met" (free text —
matching it automatically would be worse than not trying; journal.review
prints the falsifier verbatim instead and asks). Also not attempted: any
LLM summarization of filing text — every alert here traces to a citable
filing/snippet/tag, and a model's fluent-but-wrong read of a filing is the
worst failure mode for a system whose whole job is flagging anomalies.
"""

from __future__ import annotations

from dataclasses import dataclass

from smallcap import dilution, quality, runway
from smallcap.journal import store
from smallcap.journal.models import Thesis
from smallcap.sec import client as sec_client
from smallcap.sec import filings
from smallcap.sec import insiders

DILUTION_YOY_THRESHOLD = 0.15
RUNWAY_QUARTERS_THRESHOLD = 4.0

DILUTION_KIND = "dilution-yoy"
RUNWAY_KIND = "runway-low"
GOING_CONCERN_KIND = "going-concern"
CASHFLOW_DIVERGENCE_KIND = "cashflow-divergence"
RESTRUCTURING_KIND = "restructuring-recurring"
INSIDER_CLUSTER_KIND = "insider-buy-cluster"
NEW_8K_ITEM_KIND = "new-8k-item"


@dataclass
class Alert:
    kind: str
    message: str
    is_new: bool  # False if the most recent stored alert of this kind already said the same thing


def check(thesis: Thesis, *, refresh: bool = False, persist: bool = True) -> list[Alert]:
    alerts: list[Alert] = []

    try:
        d = dilution.compute(thesis.ticker, years=2, refresh=refresh)
        if len(d.adjusted_shares) >= 2 and d.adjusted_shares[0] > 0:
            yoy = d.adjusted_shares[-1] / d.adjusted_shares[0] - 1
            if yoy > DILUTION_YOY_THRESHOLD:
                msg = f"share count up {yoy:+.1%} over the lookback window (split-adjusted, {d.dates[0]} -> {d.dates[-1]})"
                alerts.append(_record(thesis.id, DILUTION_KIND, msg, persist))
    except Exception as exc:  # noqa: BLE001 — data-source gaps shouldn't break a review
        alerts.append(Alert(kind=DILUTION_KIND, message=f"could not check dilution: {exc}", is_new=False))

    try:
        r = runway.compute(thesis.ticker, refresh=refresh)
        if not r.profitable and r.runway_quarters_ttm is not None and r.runway_quarters_ttm < RUNWAY_QUARTERS_THRESHOLD:
            msg = f"runway is {r.runway_quarters_ttm:.1f} quarters (TTM avg burn), under the {RUNWAY_QUARTERS_THRESHOLD:.0f}-quarter threshold"
            alerts.append(_record(thesis.id, RUNWAY_KIND, msg, persist))
    except Exception as exc:  # noqa: BLE001
        alerts.append(Alert(kind=RUNWAY_KIND, message=f"could not check runway: {exc}", is_new=False))

    try:
        cik = sec_client.cik_for_ticker(thesis.ticker, refresh=refresh)
        gc = filings.going_concern_newly_appeared(cik, refresh=refresh)
        if gc is not None and gc.checked and gc.found:
            msg = f"going-concern language newly appeared in {gc.filing.form} filed {gc.filing.filing_date}: ...{gc.snippet}..."
            alerts.append(_record(thesis.id, GOING_CONCERN_KIND, msg, persist))
    except Exception as exc:  # noqa: BLE001
        alerts.append(Alert(kind=GOING_CONCERN_KIND, message=f"could not check going-concern language: {exc}", is_new=False))

    try:
        div = quality.cash_flow_vs_net_income(thesis.ticker, refresh=refresh)
        if div.divergent:
            alerts.append(_record(thesis.id, CASHFLOW_DIVERGENCE_KIND, div.note, persist))
    except Exception as exc:  # noqa: BLE001
        alerts.append(Alert(kind=CASHFLOW_DIVERGENCE_KIND, message=f"could not check cash flow vs. net income: {exc}", is_new=False))

    try:
        r = quality.restructuring_recurrence(thesis.ticker, refresh=refresh)
        if r.recurring:
            years = ", ".join(f"{d[:4]} (${v:,.0f})" for d, v in r.annual_charges[-r.consecutive_years:])
            msg = f"restructuring charges reported {r.consecutive_years} consecutive fiscal years ({years}, tag {r.tag_used})"
            alerts.append(_record(thesis.id, RESTRUCTURING_KIND, msg, persist))
    except Exception as exc:  # noqa: BLE001
        alerts.append(Alert(kind=RESTRUCTURING_KIND, message=f"could not check restructuring recurrence: {exc}", is_new=False))

    try:
        cik = sec_client.cik_for_ticker(thesis.ticker, refresh=refresh)
        txs = insiders.recent_transactions(cik, limit=30, refresh=refresh)
        clusters = insiders.cluster_buys(txs)
        if clusters:
            c = clusters[-1]  # most recent cluster only — history repeats the same finding otherwise
            msg = f"{len(c.owners)} insiders bought on the open market between {c.start_date} and {c.end_date}: {', '.join(c.owners)}"
            alerts.append(_record(thesis.id, INSIDER_CLUSTER_KIND, msg, persist))
    except Exception as exc:  # noqa: BLE001
        alerts.append(Alert(kind=INSIDER_CLUSTER_KIND, message=f"could not check insider buy clusters: {exc}", is_new=False))

    try:
        cik = sec_client.cik_for_ticker(thesis.ticker, refresh=refresh)
        n8k = filings.new_8k_item_types(cik, refresh=refresh)
        if n8k.checked and n8k.new_items:
            labels = ", ".join(f"{item} ({filings.ITEM_8K_LABELS.get(item, 'other')})" for item in sorted(n8k.new_items))
            msg = f"8-K filed {n8k.filing.filing_date} discloses an item type not seen in this filer's recent 8-Ks: {labels}"
            alerts.append(_record(thesis.id, NEW_8K_ITEM_KIND, msg, persist))
    except Exception as exc:  # noqa: BLE001
        alerts.append(Alert(kind=NEW_8K_ITEM_KIND, message=f"could not check for new 8-K item types: {exc}", is_new=False))

    return alerts


def _record(thesis_id: str, kind: str, message: str, persist: bool) -> Alert:
    body = f"{kind}: {message}"
    latest = store.latest_alert_of_kind(thesis_id, kind)
    is_new = latest is None or latest.body != body
    if persist and is_new:
        store.followup_add(thesis_id, body, kind="alert")
    return Alert(kind=kind, message=message, is_new=is_new)
