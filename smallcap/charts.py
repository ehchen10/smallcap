"""Matplotlib chart helpers -> PNG files under CONFIG's charts dir."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless; CLI tool, no display
import matplotlib.pyplot as plt
import pandas as pd

from smallcap.config import CHARTS_DIR, ensure_dirs


def dilution_chart(
    ticker: str,
    adjusted: pd.Series,
    raw: pd.Series,
    split_dates: list[str],
    price: pd.Series | None = None,
) -> Path:
    """Split-adjusted vs raw share count, with split events marked and an
    optional price overlay on a second axis."""
    ensure_dirs()
    fig, ax1 = plt.subplots(figsize=(10, 6))

    ax1.plot(adjusted.index, adjusted.values, label="split-adjusted shares", color="#1f77b4", linewidth=2)
    ax1.plot(raw.index, raw.values, label="raw (as reported)", color="#999999", linestyle="--", linewidth=1)
    ax1.set_ylabel("shares outstanding")
    ax1.set_xlabel("date")

    # Pin the x-axis to the actual data window: a split from decades before the
    # lookback window (e.g. AAPL's 1987 split) would otherwise force matplotlib
    # to autoscale to include it, squashing the real data into a sliver.
    x_min, x_max = adjusted.index.min(), adjusted.index.max()
    ax1.set_xlim(x_min, x_max)

    # A serial-diluting small cap with a big reverse split mid-window (the
    # exact case this tool exists for) can span 3+ orders of magnitude — on a
    # linear axis the early, smaller values are invisible. Verified on OGEN:
    # without log scale the split-adjusted line reads as flat zero for years.
    positive = adjusted[adjusted > 0]
    if not positive.empty and positive.max() / positive.min() > 100:
        ax1.set_yscale("log")

    for d in split_dates:
        ts = pd.Timestamp(d)
        if x_min <= ts <= x_max:
            ax1.axvline(ts, color="#d62728", linestyle=":", alpha=0.6)

    lines, labels = ax1.get_legend_handles_labels()

    if price is not None and not price.empty:
        ax2 = ax1.twinx()
        ax2.plot(price.index, price.values, color="#2ca02c", alpha=0.5, linewidth=1)
        ax2.set_ylabel("price ($)", color="#2ca02c")
        lines.append(plt.Line2D([0], [0], color="#2ca02c", alpha=0.5))
        labels.append("price")

    ax1.legend(lines, labels, loc="upper left")
    ax1.set_title(f"{ticker}: dilution (red dotted = detected split)")
    fig.tight_layout()

    out = CHARTS_DIR / f"{ticker.upper()}_dilution.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


_SERIES_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b"]


def calibration_chart(series: dict[str, list[dict]], *, out_name: str = "prediction_calibration.png") -> Path:
    """Calibration curve, one or more labelled series overlaid. Each point is
    annotated with its bin's n — with per-source splitting the per-bucket n
    gets small fast, and an unlabelled point at n=1 reads as signal when it's
    noise."""
    ensure_dirs()
    fig, ax = plt.subplots(figsize=(7, 7))
    ax.plot([0, 1], [0, 1], color="#999999", linestyle="--", label="perfect calibration", zorder=1)

    for i, (label, bins) in enumerate(series.items()):
        color = _SERIES_COLORS[i % len(_SERIES_COLORS)]
        pts = [b for b in bins if b["n"] > 0]
        xs = [b["mean_predicted"] for b in pts]
        ys = [b["observed_freq"] for b in pts]
        sizes = [20 + 10 * b["n"] for b in pts]
        total_n = sum(b["n"] for b in pts)
        ax.scatter(xs, ys, s=sizes, color=color, zorder=3, label=f"{label} (n={total_n})")
        for b in pts:
            ax.annotate(str(b["n"]), (b["mean_predicted"], b["observed_freq"]), fontsize=7, color=color, xytext=(4, 4), textcoords="offset points")

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("mean predicted probability")
    ax.set_ylabel("observed frequency")
    ax.set_title("Calibration")
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()

    out = CHARTS_DIR / out_name
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def reliability_chart(bins: list[dict]) -> Path:
    """10-bin calibration curve: predicted probability vs observed frequency."""
    return calibration_chart({"all": bins})
