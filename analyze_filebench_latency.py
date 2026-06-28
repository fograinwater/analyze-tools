#!/usr/bin/env python3
"""Analyze Filebench lathist output for read latency and backend miss ratio."""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


BREAKDOWN_RE = re.compile(
    r"^\s*(?P<time>\d+(?:\.\d+)?):\s+Per-Operation Breakdown\s*$"
)
RUNNING_RE = re.compile(r"^\s*(?P<time>\d+(?:\.\d+)?):\s+Running\.\.\.\s*$")
OP_RE = re.compile(
    r"^(?P<op>\S+)\s+"
    r"(?P<ops>\d+)ops\s+"
    r"(?P<ops_s>[-+]?\d+(?:\.\d+)?)ops/s\s+"
    r"(?P<mb_s>[-+]?\d+(?:\.\d+)?)MB/s\s+"
    r"(?P<kb_s>[-+]?\d+(?:\.\d+)?)KB/s\s+"
    r"(?P<avg_ms>[-+]?\d+(?:\.\d+)?)ms/op\s+"
    r"min~max\s+\[(?P<min_ms>[-+]?\d+(?:\.\d+)?)ms\s+-\s*"
    r"(?P<max_ms>[-+]?\d+(?:\.\d+)?)ms\]\s+"
    r"P90\[(?P<p90_low_ms>[-+]?\d+(?:\.\d+)?)ms\s+-\s*"
    r"(?P<p90_high_ms>[-+]?\d+(?:\.\d+)?)ms\]\s+"
    r"\[(?P<hist>[0-9 ]+)\]\s*$"
)


@dataclass(frozen=True)
class Snapshot:
    time_s: float
    op: str
    ops: int
    ops_s: float
    mb_s: float
    kb_s: float
    avg_ms: float
    min_ms: float
    max_ms: float
    p90_low_ms: float
    p90_high_ms: float
    hist: tuple[int, ...]


@dataclass(frozen=True)
class ParsedLog:
    snapshots: list[Snapshot]
    origin_time_s: float


def bucket_lower_ms(index: int) -> float:
    if index <= 0:
        return 0.0
    return (2 ** (index - 1)) / 1_000_000.0


def bucket_upper_ms(index: int) -> float:
    return (2**index) / 1_000_000.0


def bucket_label(index: int) -> str:
    low = bucket_lower_ms(index)
    high = bucket_upper_ms(index)
    return f"[{low:.6g}, {high:.6g})"


def first_bucket_crossing(threshold_ms: float, bucket_count: int) -> int:
    for index in range(bucket_count):
        if bucket_upper_ms(index) > threshold_ms:
            return index
    return bucket_count


def parse_log(path: Path, op_name: str) -> ParsedLog:
    snapshots: list[Snapshot] = []
    current_time: float | None = None
    running_time: float | None = None

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            running_match = RUNNING_RE.match(line)
            if running_match and running_time is None:
                running_time = float(running_match.group("time"))
                continue

            breakdown_match = BREAKDOWN_RE.match(line)
            if breakdown_match:
                current_time = float(breakdown_match.group("time"))
                continue

            if current_time is None:
                continue

            op_match = OP_RE.match(line)
            if not op_match or op_match.group("op") != op_name:
                continue

            hist = tuple(int(value) for value in op_match.group("hist").split())
            snapshots.append(
                Snapshot(
                    time_s=current_time,
                    op=op_match.group("op"),
                    ops=int(op_match.group("ops")),
                    ops_s=float(op_match.group("ops_s")),
                    mb_s=float(op_match.group("mb_s")),
                    kb_s=float(op_match.group("kb_s")),
                    avg_ms=float(op_match.group("avg_ms")),
                    min_ms=float(op_match.group("min_ms")),
                    max_ms=float(op_match.group("max_ms")),
                    p90_low_ms=float(op_match.group("p90_low_ms")),
                    p90_high_ms=float(op_match.group("p90_high_ms")),
                    hist=hist,
                )
            )

    if not snapshots:
        raise ValueError(f"No operation snapshots named {op_name!r} were found in {path}")

    origin_time = running_time if running_time is not None else snapshots[0].time_s
    return ParsedLog(snapshots=snapshots, origin_time_s=origin_time)


def infer_backend_threshold_ms(
    final_hist: Iterable[int],
    *,
    min_gap_buckets: int,
    min_backend_ms: float,
) -> tuple[float, str]:
    hist = list(final_hist)
    nonzero = [index for index, count in enumerate(hist) if count > 0]
    if not nonzero:
        raise ValueError("The final histogram is empty; cannot infer a backend threshold")

    candidates: list[tuple[int, int, int]] = []
    for left, right in zip(nonzero, nonzero[1:]):
        gap = right - left
        if gap >= min_gap_buckets and bucket_lower_ms(right) >= min_backend_ms:
            candidates.append((gap, left, right))

    if candidates:
        gap, left, right = max(candidates, key=lambda item: (item[0], item[2]))
        threshold = bucket_lower_ms(right)
        reason = (
            f"largest empty bucket gap: bucket {left} -> {right} "
            f"(gap={gap}, threshold={threshold:.3f} ms)"
        )
        return threshold, reason

    high_buckets = [
        index for index in nonzero if bucket_lower_ms(index) >= min_backend_ms
    ]
    if high_buckets:
        index = high_buckets[0]
        threshold = bucket_lower_ms(index)
        reason = (
            f"no large gap found; using first non-empty bucket above "
            f"{min_backend_ms:.3f} ms: bucket {index}"
        )
        return threshold, reason

    raise ValueError(
        "Could not infer a backend threshold. Re-run with --miss-threshold-ms."
    )


def sum_backend(hist: Iterable[int], miss_start_bucket: int) -> int:
    return sum(list(hist)[miss_start_bucket:])


def subtract_hist(cur: Iterable[int], prev: Iterable[int]) -> list[int]:
    return [max(0, c - p) for c, p in zip(cur, prev)]


def write_snapshot_csv(
    snapshots: list[Snapshot],
    output_path: Path,
    origin_time_s: float,
    miss_start_bucket: int,
) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "time_s",
                "elapsed_s",
                "elapsed_h",
                "ops_cum",
                "ops_s",
                "mb_s",
                "kb_s",
                "avg_ms",
                "min_ms",
                "max_ms",
                "p90_low_ms",
                "p90_high_ms",
                "backend_reads_cum_est",
                "backend_ratio_cum",
            ]
        )
        for snap in snapshots:
            backend_cum = sum_backend(snap.hist, miss_start_bucket)
            ratio = backend_cum / snap.ops if snap.ops else math.nan
            elapsed = snap.time_s - origin_time_s
            writer.writerow(
                [
                    f"{snap.time_s:.3f}",
                    f"{elapsed:.3f}",
                    f"{elapsed / 3600.0:.6f}",
                    snap.ops,
                    f"{snap.ops_s:.6f}",
                    f"{snap.mb_s:.6f}",
                    f"{snap.kb_s:.6f}",
                    f"{snap.avg_ms:.6f}",
                    f"{snap.min_ms:.6f}",
                    f"{snap.max_ms:.6f}",
                    f"{snap.p90_low_ms:.6f}",
                    f"{snap.p90_high_ms:.6f}",
                    backend_cum,
                    "" if math.isnan(ratio) else f"{ratio:.9f}",
                ]
            )


def write_interval_csv(
    snapshots: list[Snapshot],
    output_path: Path,
    origin_time_s: float,
    miss_start_bucket: int,
) -> list[dict[str, float | int]]:
    rows: list[dict[str, float | int]] = []
    prev: Snapshot | None = None

    for snap in snapshots:
        if prev is None or snap.ops < prev.ops:
            start_time = origin_time_s
            total_delta = snap.ops
            backend_delta = sum_backend(snap.hist, miss_start_bucket)
        else:
            start_time = prev.time_s
            total_delta = snap.ops - prev.ops
            backend_delta = sum_backend(
                subtract_hist(snap.hist, prev.hist), miss_start_bucket
            )

        backend_cum = sum_backend(snap.hist, miss_start_bucket)
        interval_ratio = (
            backend_delta / total_delta if total_delta > 0 else math.nan
        )
        cumulative_ratio = backend_cum / snap.ops if snap.ops > 0 else math.nan
        rows.append(
            {
                "start_time_s": start_time,
                "end_time_s": snap.time_s,
                "elapsed_start_s": start_time - origin_time_s,
                "elapsed_end_s": snap.time_s - origin_time_s,
                "elapsed_mid_s": ((start_time + snap.time_s) / 2.0)
                - origin_time_s,
                "total_reads_delta": total_delta,
                "backend_reads_delta_est": backend_delta,
                "backend_ratio_delta": interval_ratio,
                "total_reads_cum": snap.ops,
                "backend_reads_cum_est": backend_cum,
                "backend_ratio_cum": cumulative_ratio,
            }
        )
        prev = snap

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "start_time_s",
                "end_time_s",
                "elapsed_start_s",
                "elapsed_end_s",
                "elapsed_mid_s",
                "total_reads_delta",
                "backend_reads_delta_est",
                "backend_ratio_delta",
                "total_reads_cum",
                "backend_reads_cum_est",
                "backend_ratio_cum",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    f"{row['start_time_s']:.3f}",
                    f"{row['end_time_s']:.3f}",
                    f"{row['elapsed_start_s']:.3f}",
                    f"{row['elapsed_end_s']:.3f}",
                    f"{row['elapsed_mid_s']:.3f}",
                    row["total_reads_delta"],
                    row["backend_reads_delta_est"],
                    ""
                    if math.isnan(row["backend_ratio_delta"])
                    else f"{row['backend_ratio_delta']:.9f}",
                    row["total_reads_cum"],
                    row["backend_reads_cum_est"],
                    ""
                    if math.isnan(row["backend_ratio_cum"])
                    else f"{row['backend_ratio_cum']:.9f}",
                ]
            )

    return rows


def write_histogram_csv(
    final_hist: tuple[int, ...],
    output_path: Path,
    miss_start_bucket: int,
) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "bucket_index",
                "lower_ms",
                "upper_ms",
                "count",
                "is_backend_est",
            ]
        )
        for index, count in enumerate(final_hist):
            writer.writerow(
                [
                    index,
                    f"{bucket_lower_ms(index):.9f}",
                    f"{bucket_upper_ms(index):.9f}",
                    count,
                    int(index >= miss_start_bucket),
                ]
            )


def nonempty_buckets(hist: Iterable[int]) -> str:
    parts = []
    for index, count in enumerate(hist):
        if count:
            parts.append(f"{index}:{bucket_label(index)}={count}")
    return ", ".join(parts)


def write_summary(
    output_path: Path,
    log_path: Path,
    final_snapshot: Snapshot,
    threshold_ms: float,
    threshold_reason: str,
    miss_start_bucket: int,
    origin_time_s: float,
) -> None:
    backend_total = sum_backend(final_snapshot.hist, miss_start_bucket)
    ratio = backend_total / final_snapshot.ops if final_snapshot.ops else math.nan
    elapsed_s = final_snapshot.time_s - origin_time_s

    lines = [
        "Filebench latency analysis",
        "=" * 28,
        f"input_log: {log_path}",
        f"operation: {final_snapshot.op}",
        f"snapshots: parsed cumulative Filebench lathist reports",
        "",
        "Final cumulative read metrics",
        f"total_reads: {final_snapshot.ops}",
        f"avg_latency_ms: {final_snapshot.avg_ms:.6f}",
        f"min_latency_ms: {final_snapshot.min_ms:.6f}",
        f"max_latency_ms: {final_snapshot.max_ms:.6f}",
        (
            "p90_latency_bucket_ms: "
            f"[{final_snapshot.p90_low_ms:.6f}, {final_snapshot.p90_high_ms:.6f})"
        ),
        f"elapsed_from_running_s: {elapsed_s:.3f}",
        f"elapsed_from_running_h: {elapsed_s / 3600.0:.6f}",
        "",
        "Backend-read estimate",
        f"miss_threshold_ms: {threshold_ms:.6f}",
        f"miss_start_bucket: {miss_start_bucket} {bucket_label(miss_start_bucket)} ms",
        f"threshold_reason: {threshold_reason}",
        f"backend_reads_est: {backend_total}",
        f"backend_to_total_ratio: {ratio:.9f}",
        "",
        "Notes",
        "- Backend reads are estimated from high-latency histogram buckets.",
        "- Filebench lathist buckets are powers of two in nanoseconds.",
        "- Interval ratios are based on deltas between cumulative reports.",
        "- Empty intervals have an empty backend_ratio_delta in the CSV.",
        "",
        "Final non-empty latency buckets",
        nonempty_buckets(final_snapshot.hist),
        "",
    ]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def import_matplotlib():
    import matplotlib
    from matplotlib import font_manager

    matplotlib.use("Agg")
    cjk_font = find_cjk_font()
    if cjk_font is not None:
        font_manager.fontManager.addfont(str(cjk_font))
        font_name = font_manager.FontProperties(fname=str(cjk_font)).get_name()
        matplotlib.rcParams["font.family"] = font_name
    else:
        matplotlib.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    matplotlib.rcParams["font.size"] = 13
    matplotlib.rcParams["axes.titlesize"] = 17
    matplotlib.rcParams["axes.labelsize"] = 15
    matplotlib.rcParams["xtick.labelsize"] = 13
    matplotlib.rcParams["ytick.labelsize"] = 13
    matplotlib.rcParams["legend.fontsize"] = 13
    import matplotlib.pyplot as plt

    return plt


def find_cjk_font() -> Path | None:
    env_font = os.environ.get("FILEBENCH_CJK_FONT")
    candidates = []
    if env_font:
        candidates.append(Path(env_font).expanduser())

    candidates.extend(
        Path(path)
        for path in [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
            "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
            "/usr/share/fonts/adobe-source-han-sans/SourceHanSansCN-Regular.otf",
            "/usr/share/fonts/source-han-sans/SourceHanSansCN-Regular.otf",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf",
        ]
    )

    for font_path in candidates:
        if font_path.exists():
            return font_path

    return None


def plot_backend_ratio(
    rows: list[dict[str, float | int]],
    output_path: Path,
    title: str,
) -> None:
    plt = import_matplotlib()

    cumulative = [row for row in rows if int(row["total_reads_cum"]) > 0]
    if not cumulative:
        return

    fig, ax = plt.subplots(figsize=(12, 5.5))
    cum_x = [float(row["elapsed_end_s"]) / 3600.0 for row in cumulative]
    cum_y = [float(row["backend_ratio_cum"]) for row in cumulative]
    ax.plot(
        cum_x,
        cum_y,
        color="#2563eb",
        linewidth=2.0,
        zorder=2,
    )

    ax.set_xlabel("从 Filebench Running 开始的运行时间（小时）")
    ax.set_ylabel("累计后端读次数/累计操作数")
    ax.set_ylim(-0.03, 1.03)
    ax.grid(True, which="major", linestyle="--", linewidth=0.6, alpha=0.35)
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def plot_latency_timeseries(
    snapshots: list[Snapshot],
    output_path: Path,
    origin_time_s: float,
    title: str,
) -> None:
    plt = import_matplotlib()

    data = [snap for snap in snapshots if snap.ops > 0]
    if not data:
        return

    x = [(snap.time_s - origin_time_s) / 3600.0 for snap in data]
    max_y = [snap.max_ms for snap in data]
    p90_low_y = [max(snap.p90_low_ms, 1e-6) for snap in data]
    p90_high_y = [max(snap.p90_high_ms, 1e-6) for snap in data]

    fig, ax = plt.subplots(figsize=(12, 5.5))
    ax.plot(x, max_y, color="#b45309", linewidth=2.0, label="最大延迟")
    ax.plot(x, p90_low_y, color="#047857", linewidth=1.4, label="P90 下界")
    ax.plot(
        x,
        p90_high_y,
        color="#059669",
        linewidth=1.4,
        linestyle="--",
        label="P90 上界",
    )
    ax.set_yscale("log")
    ax.set_xlabel("从 Filebench Running 开始的运行时间（小时）")
    ax.set_ylabel("延迟（ms，对数坐标）")
    ax.grid(True, which="both", linestyle="--", linewidth=0.6, alpha=0.35)
    ax.legend(loc="best")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def plot_histogram(
    final_hist: tuple[int, ...],
    output_path: Path,
    threshold_ms: float,
    miss_start_bucket: int,
    title: str,
) -> None:
    plt = import_matplotlib()

    indices = [index for index, count in enumerate(final_hist) if count > 0]
    if not indices:
        return

    x = []
    widths = []
    y = []
    colors = []
    for index in indices:
        low = bucket_lower_ms(index)
        high = bucket_upper_ms(index)
        if low <= 0:
            low = high / 2.0
        x.append(math.sqrt(low * high))
        widths.append(high - low)
        y.append(final_hist[index])
        colors.append("#dc2626" if index >= miss_start_bucket else "#2563eb")

    fig, ax = plt.subplots(figsize=(12, 5.5))
    ax.bar(x, y, width=widths, color=colors, alpha=0.8, align="center")
    ax.axvline(
        threshold_ms,
        color="#111827",
        linewidth=1.5,
        linestyle="--",
        label=f"后端读阈值 {threshold_ms:.3g} ms",
    )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("读延迟（ms，对数坐标）")
    ax.set_ylabel("请求数（对数坐标）")
    ax.grid(True, which="both", linestyle="--", linewidth=0.6, alpha=0.35)
    ax.legend(loc="best")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Parse Filebench lathist logs, summarize read latency, and plot "
            "estimated backend-read ratio over time."
        )
    )
    parser.add_argument("logfile", type=Path, help="Filebench run log")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for CSV, summary, and PNG outputs",
    )
    parser.add_argument(
        "--op",
        default="rd",
        help="Filebench operation name to analyze (default: rd)",
    )
    parser.add_argument(
        "--miss-threshold-ms",
        type=float,
        default=None,
        help=(
            "Manual latency threshold for estimating backend reads. "
            "Defaults to auto-detecting a high-latency histogram cluster."
        ),
    )
    parser.add_argument(
        "--auto-min-gap-buckets",
        type=int,
        default=4,
        help="Minimum empty bucket gap used by auto threshold detection",
    )
    parser.add_argument(
        "--auto-min-backend-ms",
        type=float,
        default=1000.0,
        help="Minimum latency considered plausible for backend reads in auto mode",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    log_path = args.logfile.expanduser().resolve()
    if not log_path.exists():
        print(f"error: log file does not exist: {log_path}", file=sys.stderr)
        return 2

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else log_path.parent / f"{log_path.stem}_analysis"
    )
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        if args.output_dir is not None:
            raise
        output_dir = (
            Path.cwd().resolve()
            / "filebench_analysis"
            / f"{log_path.stem}_analysis"
        )
        output_dir.mkdir(parents=True, exist_ok=True)

    parsed = parse_log(log_path, args.op)
    snapshots = parsed.snapshots
    final_snapshot = snapshots[-1]

    if args.miss_threshold_ms is None:
        threshold_ms, threshold_reason = infer_backend_threshold_ms(
            final_snapshot.hist,
            min_gap_buckets=args.auto_min_gap_buckets,
            min_backend_ms=args.auto_min_backend_ms,
        )
    else:
        threshold_ms = args.miss_threshold_ms
        threshold_reason = "manual threshold from --miss-threshold-ms"

    miss_start_bucket = first_bucket_crossing(threshold_ms, len(final_snapshot.hist))

    summary_path = output_dir / "summary.txt"
    snapshot_csv = output_dir / "read_latency_timeseries.csv"
    interval_csv = output_dir / "backend_ratio_timeseries.csv"
    histogram_csv = output_dir / "read_latency_histogram.csv"
    backend_ratio_plot = output_dir / "backend_read_ratio_over_time.png"
    latency_plot = output_dir / "read_latency_max_p90_over_time.png"
    histogram_plot = output_dir / "read_latency_histogram.png"

    write_snapshot_csv(
        snapshots, snapshot_csv, parsed.origin_time_s, miss_start_bucket
    )
    interval_rows = write_interval_csv(
        snapshots, interval_csv, parsed.origin_time_s, miss_start_bucket
    )
    write_histogram_csv(final_snapshot.hist, histogram_csv, miss_start_bucket)
    write_summary(
        summary_path,
        log_path,
        final_snapshot,
        threshold_ms,
        threshold_reason,
        miss_start_bucket,
        parsed.origin_time_s,
    )

    try:
        plot_backend_ratio(
            interval_rows,
            backend_ratio_plot,
            "后端读比例随时间变化趋势图",
        )
        plot_latency_timeseries(
            snapshots,
            latency_plot,
            parsed.origin_time_s,
            "读延迟最大值和 P90 随时间变化趋势图",
        )
        plot_histogram(
            final_snapshot.hist,
            histogram_plot,
            threshold_ms,
            miss_start_bucket,
            "最终读延迟分布",
        )
    except ImportError as exc:
        print(f"warning: matplotlib is unavailable, skipped PNG plots: {exc}")

    backend_total = sum_backend(final_snapshot.hist, miss_start_bucket)
    ratio = backend_total / final_snapshot.ops if final_snapshot.ops else math.nan
    print(f"output_dir = {output_dir}")
    print(f"summary = {summary_path}")
    print(f"read_latency_timeseries_csv = {snapshot_csv}")
    print(f"backend_ratio_timeseries_csv = {interval_csv}")
    print(f"read_latency_histogram_csv = {histogram_csv}")
    print(f"backend_ratio_plot = {backend_ratio_plot}")
    print(f"read_latency_max_p90_plot = {latency_plot}")
    print(f"read_latency_histogram_plot = {histogram_plot}")
    print(f"total_reads = {final_snapshot.ops}")
    print(f"max_latency_ms = {final_snapshot.max_ms:.6f}")
    print(
        "p90_latency_bucket_ms = "
        f"[{final_snapshot.p90_low_ms:.6f}, {final_snapshot.p90_high_ms:.6f})"
    )
    print(f"miss_threshold_ms = {threshold_ms:.6f}")
    print(f"backend_reads_est = {backend_total}")
    print(f"backend_to_total_ratio = {ratio:.9f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
