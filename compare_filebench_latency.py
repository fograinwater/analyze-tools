#!/usr/bin/env python3
"""Compare backend-read ratios from two Filebench latency logs."""

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


DEFAULT_LOGS = [
    Path(
        "/home/ttt/filebench-use-case/filebenchRunLog/"
        "filebench_run_log_medfs_rand_read_30-1MB-1TB-hot167G_20260625-011858.log"
    ),
    Path(
        "/home/ttt/filebench-use-case/filebenchRunLog/"
        "filebench_run_log_cachefs_on_zlfs_medfs_rand_read_30-64KB-1TB_20260120-123004.log"
    ),
]

DEFAULT_LABELS = ["原始方案", "热数据缓存方案"]


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


def find_cjk_font() -> Path | None:
    env_font = os.environ.get("FILEBENCH_CJK_FONT")
    candidates = []
    if env_font:
        candidates.append(Path(env_font))
    candidates.extend(
        [
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf"),
            Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            Path("/usr/share/fonts/opentype/adobe-source-han-sans/SourceHanSansCN-Regular.otf"),
            Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


@dataclass(frozen=True)
class CompareSeries:
    label: str
    log_path: Path
    threshold_ms: float
    threshold_reason: str
    miss_start_bucket: int
    elapsed_h: list[float]
    ratios: list[float]
    total_reads: int
    backend_reads: int
    final_ratio: float
    max_latency_ms: float
    p90_low_ms: float
    p90_high_ms: float


def configure_matplotlib():
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


def build_series(
    log_path: Path,
    label: str,
    op_name: str,
    manual_threshold_ms: float | None,
    auto_min_gap_buckets: int,
    auto_min_backend_ms: float,
) -> CompareSeries:
    parsed = parse_log(log_path, op_name)
    final_snapshot = parsed.snapshots[-1]

    if manual_threshold_ms is None:
        threshold_ms, threshold_reason = infer_backend_threshold_ms(
            final_snapshot.hist,
            min_gap_buckets=auto_min_gap_buckets,
            min_backend_ms=auto_min_backend_ms,
        )
    else:
        threshold_ms = manual_threshold_ms
        threshold_reason = "manual threshold from --miss-threshold-ms"

    miss_start_bucket = first_bucket_crossing(threshold_ms, len(final_snapshot.hist))
    elapsed_h: list[float] = []
    ratios: list[float] = []

    for snap in parsed.snapshots:
        if snap.ops <= 0:
            continue
        backend_reads = sum_backend(snap.hist, miss_start_bucket)
        elapsed_h.append((snap.time_s - parsed.origin_time_s) / 3600.0)
        ratios.append(backend_reads / snap.ops)

    backend_total = sum_backend(final_snapshot.hist, miss_start_bucket)
    final_ratio = backend_total / final_snapshot.ops if final_snapshot.ops else math.nan

    return CompareSeries(
        label=label,
        log_path=log_path,
        threshold_ms=threshold_ms,
        threshold_reason=threshold_reason,
        miss_start_bucket=miss_start_bucket,
        elapsed_h=elapsed_h,
        ratios=ratios,
        total_reads=final_snapshot.ops,
        backend_reads=backend_total,
        final_ratio=final_ratio,
        max_latency_ms=final_snapshot.max_ms,
        p90_low_ms=final_snapshot.p90_low_ms,
        p90_high_ms=final_snapshot.p90_high_ms,
    )


def plot_compare(series_list: list[CompareSeries], output_path: Path) -> None:
    plt = configure_matplotlib()
    fig, ax = plt.subplots(figsize=(12, 5.5))

    colors = ["#2563eb", "#dc2626", "#059669", "#7c3aed"]
    linestyles = ["-", "-", "--", "-."]

    for index, series in enumerate(series_list):
        if not series.elapsed_h:
            continue
        final_pct = series.final_ratio * 100.0
        label = f"{series.label}（最终 {final_pct:.2f}%）"
        ax.plot(
            series.elapsed_h,
            series.ratios,
            color=colors[index % len(colors)],
            linestyle=linestyles[index % len(linestyles)],
            linewidth=2.4,
            label=label,
        )

    ax.set_title("(gamma read) 后端读比例随时间变化趋势对比图")
    ax.set_xlabel("从 Filebench Running 开始的运行时间（小时）")
    ax.set_ylabel("后端读次数/总操作数")
    ax.set_ylim(-0.03, 1.03)
    ax.grid(True, which="major", linestyle="--", linewidth=0.6, alpha=0.35)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def write_summary(series_list: list[CompareSeries], output_path: Path) -> None:
    lines = [
        "Filebench backend-read ratio comparison",
        "=" * 42,
        "",
    ]

    for series in series_list:
        lines.extend(
            [
                f"[{series.label}]",
                f"log_path: {series.log_path}",
                f"total_reads: {series.total_reads}",
                f"backend_reads_est: {series.backend_reads}",
                f"backend_to_total_ratio: {series.final_ratio:.9f}",
                f"max_latency_ms: {series.max_latency_ms:.6f}",
                (
                    "p90_latency_bucket_ms: "
                    f"[{series.p90_low_ms:.6f}, {series.p90_high_ms:.6f})"
                ),
                f"miss_threshold_ms: {series.threshold_ms:.6f}",
                (
                    "miss_start_bucket: "
                    f"{series.miss_start_bucket} {bucket_label(series.miss_start_bucket)} ms"
                ),
                f"threshold_reason: {series.threshold_reason}",
                "",
            ]
        )

    if len(series_list) == 2:
        base, cached = series_list
        ratio_delta = cached.final_ratio - base.final_ratio
        relative = (
            ratio_delta / base.final_ratio
            if base.final_ratio and not math.isnan(base.final_ratio)
            else math.nan
        )
        lines.extend(
            [
                "[对比]",
                (
                    "final_ratio_delta_cached_minus_base: "
                    f"{ratio_delta:.9f}"
                ),
                (
                    "relative_delta_vs_base: "
                    f"{relative:.9f}"
                    if not math.isnan(relative)
                    else "relative_delta_vs_base: nan"
                ),
                "",
            ]
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")


def write_timeseries_csv(series_list: list[CompareSeries], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["label", "elapsed_h", "backend_ratio_cum"])
        for series in series_list:
            for elapsed_h, ratio in zip(series.elapsed_h, series.ratios):
                writer.writerow([series.label, f"{elapsed_h:.9f}", f"{ratio:.9f}"])


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare cumulative backend-read ratios from two Filebench logs."
    )
    parser.add_argument(
        "logs",
        nargs="*",
        type=Path,
        help="Two Filebench logs. If omitted, uses the two default logs.",
    )
    parser.add_argument(
        "--labels",
        nargs=2,
        default=DEFAULT_LABELS,
        metavar=("LABEL1", "LABEL2"),
        help="Curve labels for the two logs",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("filebench_analysis/compare_backend_ratio"),
        help="Output directory for the comparison plot and CSV files",
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
        help="Use one manual latency threshold for both logs",
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

    if not args.logs:
        log_paths = DEFAULT_LOGS
    elif len(args.logs) == 2:
        log_paths = args.logs
    else:
        print("error: provide exactly two logs, or omit logs to use defaults", file=sys.stderr)
        return 2

    log_paths = [path.expanduser().resolve() for path in log_paths]
    for path in log_paths:
        if not path.exists():
            print(f"error: log file does not exist: {path}", file=sys.stderr)
            return 2

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    series_list = [
        build_series(
            log_path=log_path,
            label=label,
            op_name=args.op,
            manual_threshold_ms=args.miss_threshold_ms,
            auto_min_gap_buckets=args.auto_min_gap_buckets,
            auto_min_backend_ms=args.auto_min_backend_ms,
        )
        for log_path, label in zip(log_paths, args.labels)
    ]

    plot_path = output_dir / "backend_read_ratio_compare.png"
    summary_path = output_dir / "compare_summary.txt"
    timeseries_path = output_dir / "compare_backend_ratio_timeseries.csv"

    plot_compare(series_list, plot_path)
    write_summary(series_list, summary_path)
    write_timeseries_csv(series_list, timeseries_path)

    print(f"output_dir = {output_dir}")
    print(f"plot = {plot_path}")
    print(f"csv = {timeseries_path}")
    print(f"summary = {summary_path}")
    for series in series_list:
        print(
            f"{series.label}: total_reads = {series.total_reads} "
            f"backend_reads_est = {series.backend_reads} "
            f"ratio = {series.final_ratio:.9f} "
            f"threshold_ms = {series.threshold_ms:.6f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
