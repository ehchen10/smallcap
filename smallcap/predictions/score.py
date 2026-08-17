"""Calibration scoring: Brier score, skill vs. base rate, reliability bins.

Brier score alone is uninterpretable — a Brier of 0.15 is good if the base
rate is 50/50 and mediocre if the base rate is 90/10 (always guessing 0.9
beats most "analysis"). The skill score normalizes against that baseline, so
a positive number means the predictions actually contain information beyond
"how often does this kind of thing happen."
"""

from __future__ import annotations

from dataclasses import dataclass

from smallcap.journal.models import Thesis
from smallcap.predictions.store import Prediction

MIN_N_FOR_RELIABILITY_CHART = 20


@dataclass
class ScoreReport:
    n: int
    brier: float | None
    base_rate: float | None
    baseline_brier: float | None
    skill_score: float | None  # 1 - brier/baseline_brier; >0 = beats base-rate guessing
    mean_predicted: float | None
    overconfidence: float | None  # mean_predicted - base_rate, sign shows direction of bias
    bins: list[dict]
    show_reliability_chart: bool


def _outcome_to_float(p: Prediction) -> float:
    return 1.0 if p.outcome == "yes" else 0.0


def score(predictions: list[Prediction]) -> ScoreReport:
    resolved = [p for p in predictions if p.status == "resolved" and p.outcome in ("yes", "no")]
    n = len(resolved)

    bins = _reliability_bins(resolved)

    if n == 0:
        return ScoreReport(
            n=0, brier=None, base_rate=None, baseline_brier=None, skill_score=None,
            mean_predicted=None, overconfidence=None, bins=bins, show_reliability_chart=False,
        )

    outcomes = [_outcome_to_float(p) for p in resolved]
    probs = [p.prob for p in resolved]

    brier = sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / n
    base_rate = sum(outcomes) / n
    baseline_brier = sum((base_rate - o) ** 2 for o in outcomes) / n
    skill_score = None if baseline_brier == 0 else 1 - brier / baseline_brier
    mean_predicted = sum(probs) / n
    overconfidence = mean_predicted - base_rate

    return ScoreReport(
        n=n,
        brier=brier,
        base_rate=base_rate,
        baseline_brier=baseline_brier,
        skill_score=skill_score,
        mean_predicted=mean_predicted,
        overconfidence=overconfidence,
        bins=bins,
        show_reliability_chart=n >= MIN_N_FOR_RELIABILITY_CHART,
    )


def _reliability_bins(resolved: list[Prediction], n_bins: int = 10) -> list[dict]:
    bins = [{"lo": i / n_bins, "hi": (i + 1) / n_bins, "n": 0, "sum_predicted": 0.0, "sum_outcome": 0.0} for i in range(n_bins)]
    for p in resolved:
        idx = min(int(p.prob * n_bins), n_bins - 1)
        b = bins[idx]
        b["n"] += 1
        b["sum_predicted"] += p.prob
        b["sum_outcome"] += _outcome_to_float(p)

    out = []
    for b in bins:
        n = b["n"]
        out.append(
            {
                "lo": b["lo"],
                "hi": b["hi"],
                "n": n,
                "mean_predicted": b["sum_predicted"] / n if n else (b["lo"] + b["hi"]) / 2,
                "observed_freq": b["sum_outcome"] / n if n else 0.0,
            }
        )
    return out


def score_by_source(predictions: list[Prediction], theses: list[Thesis]) -> dict[str, ScoreReport]:
    """Bucket predictions by their linked thesis's ``source`` field and score
    each bucket independently. The single most actionable number this whole
    system can produce: if social-media-sourced ideas score worse than
    own-observation ideas, that's worth knowing on its own, separate from the
    aggregate. Predictions with no ``thesis_id`` (or one pointing at a thesis
    not in ``theses``) land in the "unattributed" bucket rather than being
    dropped.
    """
    thesis_source = {t.id: t.source for t in theses}
    buckets: dict[str, list[Prediction]] = {}
    for p in predictions:
        source = thesis_source.get(p.thesis_id, "unattributed") if p.thesis_id else "unattributed"
        buckets.setdefault(source, []).append(p)
    return {source: score(preds) for source, preds in buckets.items()}


def brier_trend(predictions: list[Prediction]) -> list[tuple[str, int, float]]:
    """Expanding-window Brier score ordered by resolution date: at each
    resolved prediction, the Brier score over every resolution up to and
    including that one. ``(resolved_at, n, brier)`` triples. Lets you see
    whether calibration is improving over time rather than just where it
    stands today."""
    resolved = sorted(
        (p for p in predictions if p.status == "resolved" and p.outcome in ("yes", "no")),
        key=lambda p: p.resolved_at,
    )
    out = []
    sq_err_sum = 0.0
    for i, p in enumerate(resolved, start=1):
        outcome = _outcome_to_float(p)
        sq_err_sum += (p.prob - outcome) ** 2
        out.append((p.resolved_at, i, sq_err_sum / i))
    return out


def format_report(r: ScoreReport) -> str:
    lines = [f"n = {r.n} resolved prediction{'s' if r.n != 1 else ''}"]
    if r.n == 0:
        lines.append("nothing resolved yet")
        return "\n".join(lines)

    lines.append(f"Brier score: {r.brier:.3f} (lower is better; 0 = perfect, 0.25 = always guess 50%)")
    lines.append(f"base rate: {r.base_rate:.0%} of resolved claims were 'yes'")
    lines.append(f"skill score vs. base-rate guessing: {r.skill_score:+.2f}" if r.skill_score is not None else "skill score: n/a (base rate is 0% or 100%)")
    bias = "overconfident" if r.overconfidence > 0.03 else ("underconfident" if r.overconfidence < -0.03 else "roughly calibrated")
    lines.append(f"mean predicted probability: {r.mean_predicted:.0%} ({bias}, {r.overconfidence:+.0%} vs. base rate)")

    if r.n < MIN_N_FOR_RELIABILITY_CHART:
        lines.append(f"\n(reliability chart suppressed below n={MIN_N_FOR_RELIABILITY_CHART} — {r.n} resolved points is noise, not a curve)")
    else:
        lines.append("\nreliability bins (predicted -> observed, n):")
        for b in r.bins:
            if b["n"] > 0:
                lines.append(f"  {b['lo']:.0%}-{b['hi']:.0%}: predicted {b['mean_predicted']:.0%} -> observed {b['observed_freq']:.0%}  (n={b['n']})")

    return "\n".join(lines)


def format_by_source_report(by_source: dict[str, ScoreReport]) -> str:
    """One line per source, sorted by n descending — buckets get small fast
    once split, so the count is load-bearing context for every Brier number
    next to it, not decoration."""
    lines = ["calibration by thesis source:"]
    for source, r in sorted(by_source.items(), key=lambda kv: -kv[1].n):
        if r.n == 0:
            lines.append(f"  {source}: no resolved predictions")
        else:
            skill = f"{r.skill_score:+.2f}" if r.skill_score is not None else "n/a"
            lines.append(f"  {source}: n={r.n}  brier={r.brier:.3f}  skill={skill}")
    return "\n".join(lines)
