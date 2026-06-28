#!/usr/bin/env python3
"""Compare Filebench-estimated and SpinFS-counter read backend ratios."""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_FILEBENCH_LOG = Path(
    "/home/ttt/filebench-use-case/filebenchRunLog/"
    "filebench_run_log_medfs_rand_read_30-1MB-1TB_20260625-191534.log"
)
DEFAULT_SPINFS_LOG = Path(
    "/home/ttt/filebench-use-case/filebenchRunLog/"
    "readopsVSreadmedfs_log_cachefs_on_zlfs_medfs_gamma_test_20260625-200753.log"
)


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
SPINFS_ROW_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+"
    r"(?P<read_ops>\d+)\s+"
    r"(?P<open_archive_cnt>\d+)\s+"
    r"(?P<pinfs_read_req_total>\S+)\s+"
    r"(?P<f2fs_phys_total>\S+)\s+"
    r"(?P<f2fs_phys_read_file_sum_waf>[-+]?\d+(?:\.\d+)?)\s*$"
)


@dataclass(frozen=True)
class Snapshot:
    time_s: float
    ops: int
    max_ms: float
    hist: tuple[int, ...]


@dataclass(frozen=True)
class ParsedFilebenchLog:
    snapshots: list[Snapshot]
    origin_time_s: float


@dataclass(frozen=True)
class SpinfsSample:
    timestamp: datetime
    elapsed_s: float
    read_ops: int
    open_archive_cnt: int
    cumulative_ratio: float


def bucket_lower_ms(index: int) -> float:
    if index <= 0:
        return 0.0
    return (2 ** (index - 1)) / 1_000_000.0


def bucket_upper_ms(index: int) -> float:
    return (2**index) / 1_000_000.0


def bucket_label(index: int) -> str:
    return f"[{bucket_lower_ms(index):.6g}, {bucket_upper_ms(index):.6g})"


def first_bucket_crossing(threshold_ms: float, bucket_count: int) -> int:
    for index in range(bucket_count):
        if bucket_upper_ms(index) > threshold_ms:
            return index
    return bucket_count


def parse_filebench_log(path: Path, op_name: str) -> ParsedFilebenchLog:
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

            snapshots.append(
                Snapshot(
                    time_s=current_time,
                    ops=int(op_match.group("ops")),
                    max_ms=float(op_match.group("max_ms")),
                    hist=tuple(int(value) for value in op_match.group("hist").split()),
                )
            )

    if not snapshots:
        raise ValueError(f"No operation snapshots named {op_name!r} were found in {path}")

    origin_time = running_time if running_time is not None else snapshots[0].time_s
    return ParsedFilebenchLog(snapshots=snapshots, origin_time_s=origin_time)


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


def parse_spinfs_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")


def parse_spinfs_log(path: Path) -> list[SpinfsSample]:
    samples: list[SpinfsSample] = []
    first_ts: datetime | None = None

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("timestamp"):
                continue

            match = SPINFS_ROW_RE.match(line)
            if not match:
                raise ValueError(f"line {line_number}: cannot parse row: {line}")

            timestamp = parse_spinfs_timestamp(match.group("timestamp"))
            if first_ts is None:
                first_ts = timestamp

            read_ops = int(match.group("read_ops"))
            open_archive_cnt = int(match.group("open_archive_cnt"))
            elapsed_s = (timestamp - first_ts).total_seconds()
            samples.append(
                SpinfsSample(
                    timestamp=timestamp,
                    elapsed_s=elapsed_s,
                    read_ops=read_ops,
                    open_archive_cnt=open_archive_cnt,
                    cumulative_ratio=open_archive_cnt / read_ops if read_ops else math.nan,
                )
            )

    if not samples:
        raise ValueError(f"no samples were found in {path}")

    return samples


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
class RatioPoint:
    elapsed_s: float
    numerator: int
    denominator: int
    ratio: float


@dataclass(frozen=True)
class RatioSeries:
    source: str
    label: str
    log_path: Path
    points: list[RatioPoint]
    final_numerator: int
    final_denominator: int
    final_ratio: float
    note: str


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
    matplotlib.rcParams["legend.fontsize"] = 12

    import matplotlib.pyplot as plt

    return plt


def build_filebench_series(
    log_path: Path,
    label: str,
    op_name: str,
    manual_threshold_ms: float | None,
    auto_min_gap_buckets: int,
    auto_min_backend_ms: float,
) -> RatioSeries:
    parsed = parse_filebench_log(log_path, op_name)
    final_snapshot = parsed.snapshots[-1]
    origin_time_s = parsed.snapshots[0].time_s

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
    points: list[RatioPoint] = []
    for snapshot in parsed.snapshots:
        backend_reads = sum_backend(snapshot.hist, miss_start_bucket)
        points.append(
            RatioPoint(
                elapsed_s=snapshot.time_s - origin_time_s,
                numerator=backend_reads,
                denominator=snapshot.ops,
                ratio=backend_reads / snapshot.ops if snapshot.ops else math.nan,
            )
        )

    backend_total = sum_backend(final_snapshot.hist, miss_start_bucket)
    final_ratio = backend_total / final_snapshot.ops if final_snapshot.ops else math.nan
    note = (
        "Filebench latency histogram estimate; "
        "time origin is the first Per-Operation Breakdown; "
        f"threshold={threshold_ms:.6f}ms, "
        f"bucket={miss_start_bucket} {bucket_label(miss_start_bucket)}ms, "
        f"reason={threshold_reason}"
    )
    return RatioSeries(
        source="filebench",
        label=label,
        log_path=log_path,
        points=points,
        final_numerator=backend_total,
        final_denominator=final_snapshot.ops,
        final_ratio=final_ratio,
        note=note,
    )


def build_spinfs_series(log_path: Path, label: str) -> RatioSeries:
    samples = parse_spinfs_log(log_path)
    points = [
        RatioPoint(
            elapsed_s=sample.elapsed_s,
            numerator=sample.open_archive_cnt,
            denominator=sample.read_ops,
            ratio=sample.cumulative_ratio,
        )
        for sample in samples
        if sample.read_ops > 0
    ]
    final = samples[-1]
    final_ratio = (
        final.open_archive_cnt / final.read_ops if final.read_ops else math.nan
    )
    return RatioSeries(
        source="spinfs",
        label=label,
        log_path=log_path,
        points=points,
        final_numerator=final.open_archive_cnt,
        final_denominator=final.read_ops,
        final_ratio=final_ratio,
        note="SpinFS direct counters: spinfs_open_archive_cnt / spinfs_read_ops",
    )


def choose_x_unit(series_list: list[RatioSeries], plot_scope: str) -> str:
    max_elapsed = max(point.elapsed_s for series in series_list for point in series.points)
    if plot_scope == "common":
        max_elapsed = min(series.points[-1].elapsed_s for series in series_list)
    return "hour" if max_elapsed > 7200 else "min"


def x_value(elapsed_s: float, x_unit: str) -> float:
    if x_unit == "hour":
        return elapsed_s / 3600.0
    return elapsed_s / 60.0


def x_label(x_unit: str, plot_scope: str) -> str:
    unit_text = "小时" if x_unit == "hour" else "分钟"
    scope_text = "共同时间范围内" if plot_scope == "common" else "全量"
    return f"从共同起点开始的运行时间（{unit_text}，{scope_text}）"


def filtered_points(series: RatioSeries, max_elapsed_s: float | None) -> list[RatioPoint]:
    if max_elapsed_s is None:
        return series.points
    return [point for point in series.points if point.elapsed_s <= max_elapsed_s]


def plot_compare(
    series_list: list[RatioSeries],
    output_path: Path,
    plot_scope: str,
    x_unit: str,
) -> None:
    plt = configure_matplotlib()
    fig, ax = plt.subplots(figsize=(11, 6))

    common_max_elapsed_s = None
    if plot_scope == "common":
        common_max_elapsed_s = min(series.points[-1].elapsed_s for series in series_list)

    colors = ["#2563eb", "#dc2626", "#059669", "#7c3aed"]
    markers = ["o", "s", "^", "D"]
    for index, series in enumerate(series_list):
        points = filtered_points(series, common_max_elapsed_s)
        if not points:
            continue
        finite_points = [point for point in points if not math.isnan(point.ratio)]
        if not finite_points:
            continue
        label = f"{series.label}（末点 {finite_points[-1].ratio * 100.0:.2f}%）"
        ax.plot(
            [x_value(point.elapsed_s, x_unit) for point in points],
            [point.ratio for point in points],
            color=colors[index % len(colors)],
            marker=markers[index % len(markers)],
            markersize=4.0,
            linewidth=2.2,
            label=label,
        )

    ax.set_title("读磁电盘次数 / 读操作数 随时间变化对比")
    ax.set_xlabel(x_label(x_unit, plot_scope))
    ax.set_ylabel("累计读磁电盘次数 / 累计读操作数")
    ax.set_xlim(left=0)
    ax.set_ylim(-0.03, 1.03)
    ax.grid(True, linestyle="--", linewidth=0.6, alpha=0.35)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def write_timeseries_csv(
    series_list: list[RatioSeries],
    output_path: Path,
    plot_scope: str,
) -> None:
    common_max_elapsed_s = None
    if plot_scope == "common":
        common_max_elapsed_s = min(series.points[-1].elapsed_s for series in series_list)

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "source",
                "label",
                "elapsed_s",
                "elapsed_min",
                "elapsed_h",
                "numerator",
                "denominator",
                "ratio",
                "plotted",
            ]
        )
        for series in series_list:
            for point in series.points:
                plotted = (
                    common_max_elapsed_s is None
                    or point.elapsed_s <= common_max_elapsed_s
                )
                writer.writerow(
                    [
                        series.source,
                        series.label,
                        f"{point.elapsed_s:.3f}",
                        f"{point.elapsed_s / 60.0:.6f}",
                        f"{point.elapsed_s / 3600.0:.9f}",
                        point.numerator,
                        point.denominator,
                        f"{point.ratio:.9f}",
                        int(plotted),
                    ]
                )


def write_summary(
    series_list: list[RatioSeries],
    output_path: Path,
    plot_scope: str,
    x_unit: str,
) -> None:
    lines = [
        "Filebench and SpinFS read backend ratio comparison",
        "=" * 54,
        "",
        f"plot_scope: {plot_scope}",
        f"x_unit: {x_unit}",
        "",
    ]
    if plot_scope == "common":
        common_max = min(series.points[-1].elapsed_s for series in series_list)
        lines.append(f"common_elapsed_s: {common_max:.3f}")
        lines.append(f"common_elapsed_min: {common_max / 60.0:.6f}")
        lines.append("")

    for series in series_list:
        last = series.points[-1]
        lines.extend(
            [
                f"[{series.label}]",
                f"source: {series.source}",
                f"log_path: {series.log_path}",
                f"points: {len(series.points)}",
                f"elapsed_s: {last.elapsed_s:.3f}",
                f"elapsed_min: {last.elapsed_s / 60.0:.6f}",
                f"final_numerator: {series.final_numerator}",
                f"final_denominator: {series.final_denominator}",
                f"final_ratio: {series.final_ratio:.9f}",
                f"note: {series.note}",
                "",
            ]
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Put Filebench-estimated backend-read ratio and SpinFS counter "
            "ratio on one plot."
        )
    )
    parser.add_argument(
        "--filebench-log",
        type=Path,
        default=DEFAULT_FILEBENCH_LOG,
        help="Filebench lathist log",
    )
    parser.add_argument(
        "--spinfs-log",
        type=Path,
        default=DEFAULT_SPINFS_LOG,
        help="readopsVSreadmedfs SpinFS counter log",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("filebench_analysis/compare_filebench_spinfs_read_ratio"),
        help="Output directory",
    )
    parser.add_argument("--op", default="rd", help="Filebench operation name")
    parser.add_argument(
        "--filebench-label",
        default="Filebench延迟估算",
        help="Legend label for the Filebench curve",
    )
    parser.add_argument(
        "--spinfs-label",
        default="SpinFS计数器",
        help="Legend label for the SpinFS curve",
    )
    parser.add_argument(
        "--plot-scope",
        choices=["common", "full"],
        default="common",
        help="Plot common duration or full duration (default: common)",
    )
    parser.add_argument(
        "--x-unit",
        choices=["auto", "min", "hour"],
        default="auto",
        help="X-axis unit (default: auto)",
    )
    parser.add_argument(
        "--miss-threshold-ms",
        type=float,
        default=None,
        help="Manual Filebench backend latency threshold in ms",
    )
    parser.add_argument(
        "--auto-min-gap-buckets",
        type=int,
        default=6,
        help="Minimum empty histogram bucket gap for automatic threshold inference",
    )
    parser.add_argument(
        "--auto-min-backend-ms",
        type=float,
        default=1000.0,
        help="Minimum backend latency in ms for automatic threshold inference",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    filebench_log = args.filebench_log.expanduser().resolve()
    spinfs_log = args.spinfs_log.expanduser().resolve()
    for path in [filebench_log, spinfs_log]:
        if not path.exists():
            print(f"error: log file does not exist: {path}", file=sys.stderr)
            return 2

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    series_list = [
        build_filebench_series(
            log_path=filebench_log,
            label=args.filebench_label,
            op_name=args.op,
            manual_threshold_ms=args.miss_threshold_ms,
            auto_min_gap_buckets=args.auto_min_gap_buckets,
            auto_min_backend_ms=args.auto_min_backend_ms,
        ),
        build_spinfs_series(spinfs_log, args.spinfs_label),
    ]

    x_unit = args.x_unit
    if x_unit == "auto":
        x_unit = choose_x_unit(series_list, args.plot_scope)

    plot_path = output_dir / f"read_backend_ratio_compare_{args.plot_scope}.png"
    csv_path = output_dir / f"read_backend_ratio_compare_{args.plot_scope}.csv"
    summary_path = output_dir / f"summary_{args.plot_scope}.txt"

    plot_compare(series_list, plot_path, args.plot_scope, x_unit)
    write_timeseries_csv(series_list, csv_path, args.plot_scope)
    write_summary(series_list, summary_path, args.plot_scope, x_unit)

    print(f"output_dir = {output_dir}")
    print(f"plot = {plot_path}")
    print(f"csv = {csv_path}")
    print(f"summary = {summary_path}")
    for series in series_list:
        print(
            f"{series.label}: final_numerator = {series.final_numerator} "
            f"final_denominator = {series.final_denominator} "
            f"final_ratio = {series.final_ratio:.9f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
