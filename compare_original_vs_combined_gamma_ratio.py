#!/usr/bin/env python3
"""Compare original Filebench ratio with combined hot-cache gamma ratio."""

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


DEFAULT_ORIGINAL_LOG = Path(
    "/home/ttt/filebench-use-case/filebenchRunLog/"
    "filebench_run_log_medfs_rand_read_30-1MB-1TB_20260625-191534.log"
)
DEFAULT_FIRST_GAMMA_LOG = Path(
    "/home/ttt/filebench-use-case/filebenchRunLog/"
    "readopsVSreadmedfs_log_cachefs_on_zlfs_medfs_gamma_test_20260625-200753.log"
)
DEFAULT_SECOND_GAMMA_LOG = Path(
    "/home/ttt/filebench-use-case/filebenchRunLog/"
    "filebench_run_log_cachefs_on_zlfs_medfs_gamma_test_20260627-001502-gamma2.log"
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


@dataclass(frozen=True)
class CombinedPoint:
    segment: str
    elapsed_s: float
    backend_reads_cum: int
    read_ops_cum: int
    ratio: float
    raw_backend_reads_cum: int
    raw_read_ops_cum: int


@dataclass(frozen=True)
class CombinedResult:
    points: list[CombinedPoint]
    first_elapsed_s: float
    first_backend_reads: int
    first_read_ops: int
    second_backend_reads_est: int
    second_read_ops: int
    final_backend_reads: int
    final_read_ops: int
    final_ratio: float
    filebench_threshold_ms: float
    filebench_threshold_reason: str
    filebench_miss_start_bucket: int


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


def build_combined_result(
    first_gamma_log: Path,
    second_gamma_log: Path,
    op_name: str,
    manual_threshold_ms: float | None,
    auto_min_gap_buckets: int,
    auto_min_backend_ms: float,
    second_start_gap_s: float,
) -> CombinedResult:
    first_samples = parse_spinfs_log(first_gamma_log)
    first_points = [
        CombinedPoint(
            segment="gamma1_spinfs_counter",
            elapsed_s=sample.elapsed_s,
            backend_reads_cum=sample.open_archive_cnt,
            read_ops_cum=sample.read_ops,
            ratio=sample.cumulative_ratio,
            raw_backend_reads_cum=sample.open_archive_cnt,
            raw_read_ops_cum=sample.read_ops,
        )
        for sample in first_samples
        if sample.read_ops > 0
    ]
    if not first_points:
        raise ValueError(f"no valid SpinFS samples in {first_gamma_log}")

    first_last = first_points[-1]
    first_elapsed_s = first_last.elapsed_s
    first_backend_reads = first_last.backend_reads_cum
    first_read_ops = first_last.read_ops_cum

    parsed_second = parse_filebench_log(second_gamma_log, op_name)
    final_snapshot = parsed_second.snapshots[-1]
    if manual_threshold_ms is None:
        threshold_ms, threshold_reason = infer_backend_threshold_ms(
            final_snapshot.hist,
            min_gap_buckets=auto_min_gap_buckets,
            min_backend_ms=auto_min_backend_ms,
        )
    else:
        threshold_ms = manual_threshold_ms
        threshold_reason = "manual threshold from --hot-miss-threshold-ms"

    miss_start_bucket = first_bucket_crossing(threshold_ms, len(final_snapshot.hist))
    second_origin_s = parsed_second.snapshots[0].time_s
    second_offset_s = first_elapsed_s + second_start_gap_s

    points = list(first_points)
    for snapshot in parsed_second.snapshots:
        if snapshot.ops <= 0:
            continue

        backend_est = sum_backend(snapshot.hist, miss_start_bucket)
        read_ops_cum = first_read_ops + snapshot.ops
        backend_reads_cum = first_backend_reads + backend_est
        points.append(
            CombinedPoint(
                segment="gamma2_filebench_estimate",
                elapsed_s=second_offset_s + (snapshot.time_s - second_origin_s),
                backend_reads_cum=backend_reads_cum,
                read_ops_cum=read_ops_cum,
                ratio=backend_reads_cum / read_ops_cum,
                raw_backend_reads_cum=backend_est,
                raw_read_ops_cum=snapshot.ops,
            )
        )

    if len(points) == len(first_points):
        raise ValueError(f"no valid Filebench snapshots in {second_gamma_log}")

    second_backend_reads_est = sum_backend(final_snapshot.hist, miss_start_bucket)
    second_read_ops = final_snapshot.ops
    final_backend_reads = first_backend_reads + second_backend_reads_est
    final_read_ops = first_read_ops + second_read_ops
    final_ratio = final_backend_reads / final_read_ops if final_read_ops else math.nan

    return CombinedResult(
        points=points,
        first_elapsed_s=first_elapsed_s,
        first_backend_reads=first_backend_reads,
        first_read_ops=first_read_ops,
        second_backend_reads_est=second_backend_reads_est,
        second_read_ops=second_read_ops,
        final_backend_reads=final_backend_reads,
        final_read_ops=final_read_ops,
        final_ratio=final_ratio,
        filebench_threshold_ms=threshold_ms,
        filebench_threshold_reason=threshold_reason,
        filebench_miss_start_bucket=miss_start_bucket,
    )


@dataclass(frozen=True)
class RatioPoint:
    elapsed_s: float
    backend_reads_cum: int
    read_ops_cum: int
    ratio: float


@dataclass(frozen=True)
class OriginalSeries:
    label: str
    log_path: Path
    points: list[RatioPoint]
    final_backend_reads: int
    final_read_ops: int
    final_ratio: float
    threshold_ms: float
    threshold_reason: str
    miss_start_bucket: int


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


def build_original_series(
    log_path: Path,
    label: str,
    op_name: str,
    manual_threshold_ms: float | None,
    auto_min_gap_buckets: int,
    auto_min_backend_ms: float,
) -> OriginalSeries:
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
        threshold_reason = "manual threshold from --original-miss-threshold-ms"

    miss_start_bucket = first_bucket_crossing(threshold_ms, len(final_snapshot.hist))
    points: list[RatioPoint] = []
    for snapshot in parsed.snapshots:
        backend_reads = sum_backend(snapshot.hist, miss_start_bucket)
        points.append(
            RatioPoint(
                elapsed_s=snapshot.time_s - origin_time_s,
                backend_reads_cum=backend_reads,
                read_ops_cum=snapshot.ops,
                ratio=backend_reads / snapshot.ops if snapshot.ops else math.nan,
            )
        )

    final_backend_reads = sum_backend(final_snapshot.hist, miss_start_bucket)
    final_ratio = (
        final_backend_reads / final_snapshot.ops if final_snapshot.ops else math.nan
    )

    return OriginalSeries(
        label=label,
        log_path=log_path,
        points=points,
        final_backend_reads=final_backend_reads,
        final_read_ops=final_snapshot.ops,
        final_ratio=final_ratio,
        threshold_ms=threshold_ms,
        threshold_reason=threshold_reason,
        miss_start_bucket=miss_start_bucket,
    )


def choose_x_unit(
    original: OriginalSeries, hot: CombinedResult, plot_scope: str
) -> str:
    if plot_scope == "common":
        max_elapsed_s = min(original.points[-1].elapsed_s, hot.points[-1].elapsed_s)
    else:
        max_elapsed_s = max(original.points[-1].elapsed_s, hot.points[-1].elapsed_s)
    return "hour" if max_elapsed_s > 7200 else "min"


def scale_for_unit(x_unit: str) -> tuple[float, str]:
    if x_unit == "hour":
        return 3600.0, "小时"
    return 60.0, "分钟"


def finite_last_ratio(points: list[RatioPoint]) -> float:
    for point in reversed(points):
        if not math.isnan(point.ratio):
            return point.ratio
    return math.nan


def finite_last_hot_ratio(points) -> float:
    for point in reversed(points):
        if not math.isnan(point.ratio):
            return point.ratio
    return math.nan


def filter_original_points(
    points: list[RatioPoint], max_elapsed_s: float | None
) -> list[RatioPoint]:
    if max_elapsed_s is None:
        return points
    return [point for point in points if point.elapsed_s <= max_elapsed_s]


def filter_hot_points(points, max_elapsed_s: float | None):
    if max_elapsed_s is None:
        return points
    return [point for point in points if point.elapsed_s <= max_elapsed_s]


def plot_compare(
    original: OriginalSeries,
    hot: CombinedResult,
    output_path: Path,
    plot_scope: str,
    x_unit: str,
    hot_label: str,
) -> None:
    plt = configure_matplotlib()
    fig, ax = plt.subplots(figsize=(11.5, 6.2))

    max_elapsed_s = None
    if plot_scope == "common":
        max_elapsed_s = min(original.points[-1].elapsed_s, hot.points[-1].elapsed_s)

    original_points = filter_original_points(original.points, max_elapsed_s)
    hot_points = filter_hot_points(hot.points, max_elapsed_s)
    scale, unit_text = scale_for_unit(x_unit)

    original_plot_ratio = finite_last_ratio(original_points)
    hot_plot_ratio = finite_last_hot_ratio(hot_points)

    ax.plot(
        [point.elapsed_s / scale for point in original_points],
        [point.ratio for point in original_points],
        color="#2563eb",
        linestyle="-",
        linewidth=2.4,
        marker="o",
        markersize=3.2,
        label=f"{original.label}（末点 {original_plot_ratio * 100.0:.2f}%）",
    )
    ax.plot(
        [point.elapsed_s / scale for point in hot_points],
        [point.ratio for point in hot_points],
        color="#dc2626",
        linestyle="--",
        linewidth=2.7,
        marker="o",
        markersize=3.4,
        label=f"{hot_label}（末点 {hot_plot_ratio * 100.0:.2f}%）",
    )

    second_start_x = hot.first_elapsed_s / scale
    if max_elapsed_s is None or hot.first_elapsed_s <= max_elapsed_s:
        ax.axvline(
            second_start_x,
            color="#374151",
            linestyle=":",
            linewidth=1.2,
            label="第二次 gamma 开始",
        )

    scope_text = "共同时间范围" if plot_scope == "common" else "全量时间范围"
    ax.set_title("原始方案与热数据缓存方案读磁电盘比例对比")
    ax.set_xlabel(f"运行时间（{unit_text}，{scope_text}）")
    ax.set_ylabel("累计读磁电盘次数 / 累计读操作数")
    ax.set_xlim(left=0)
    ax.set_ylim(-0.03, 1.03)
    ax.grid(True, linestyle="--", linewidth=0.6, alpha=0.35)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def write_csv(
    original: OriginalSeries,
    hot: CombinedResult,
    output_path: Path,
    plot_scope: str,
) -> None:
    max_elapsed_s = None
    if plot_scope == "common":
        max_elapsed_s = min(original.points[-1].elapsed_s, hot.points[-1].elapsed_s)

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "series",
                "segment",
                "elapsed_s",
                "elapsed_min",
                "elapsed_h",
                "backend_reads_cum",
                "read_ops_cum",
                "ratio",
                "plotted",
            ]
        )

        for point in original.points:
            plotted = max_elapsed_s is None or point.elapsed_s <= max_elapsed_s
            writer.writerow(
                [
                    "original_filebench",
                    "original_filebench_estimate",
                    f"{point.elapsed_s:.3f}",
                    f"{point.elapsed_s / 60.0:.6f}",
                    f"{point.elapsed_s / 3600.0:.9f}",
                    point.backend_reads_cum,
                    point.read_ops_cum,
                    f"{point.ratio:.9f}",
                    int(plotted),
                ]
            )

        for point in hot.points:
            plotted = max_elapsed_s is None or point.elapsed_s <= max_elapsed_s
            writer.writerow(
                [
                    "hot_cache_combined",
                    point.segment,
                    f"{point.elapsed_s:.3f}",
                    f"{point.elapsed_s / 60.0:.6f}",
                    f"{point.elapsed_s / 3600.0:.9f}",
                    point.backend_reads_cum,
                    point.read_ops_cum,
                    f"{point.ratio:.9f}",
                    int(plotted),
                ]
            )


def write_summary(
    original: OriginalSeries,
    hot: CombinedResult,
    output_path: Path,
    plot_scope: str,
    x_unit: str,
    original_log: Path,
    first_gamma_log: Path,
    second_gamma_log: Path,
) -> None:
    lines = [
        "Original vs combined gamma hot-cache ratio comparison",
        "=" * 59,
        "",
        f"plot_scope: {plot_scope}",
        f"x_unit: {x_unit}",
        "",
        f"original_log: {original_log}",
        f"first_gamma_log: {first_gamma_log}",
        f"second_gamma_log: {second_gamma_log}",
        "",
    ]

    if plot_scope == "common":
        common_elapsed_s = min(original.points[-1].elapsed_s, hot.points[-1].elapsed_s)
        lines.extend(
            [
                f"common_elapsed_s: {common_elapsed_s:.3f}",
                f"common_elapsed_min: {common_elapsed_s / 60.0:.6f}",
                "",
            ]
        )

    lines.extend(
        [
            "[original Filebench estimate]",
            f"elapsed_s: {original.points[-1].elapsed_s:.3f}",
            f"backend_reads_est: {original.final_backend_reads}",
            f"rd_ops: {original.final_read_ops}",
            f"final_ratio: {original.final_ratio:.9f}",
            f"miss_threshold_ms: {original.threshold_ms:.6f}",
            (
                "miss_start_bucket: "
                f"{original.miss_start_bucket} "
                f"{bucket_label(original.miss_start_bucket)} ms"
            ),
            f"threshold_reason: {original.threshold_reason}",
            "",
            "[hot cache combined]",
            f"gamma1_backend_reads: {hot.first_backend_reads}",
            f"gamma1_read_ops: {hot.first_read_ops}",
            f"gamma2_backend_reads_est: {hot.second_backend_reads_est}",
            f"gamma2_rd_ops: {hot.second_read_ops}",
            f"combined_final_backend_reads: {hot.final_backend_reads}",
            f"combined_final_read_ops: {hot.final_read_ops}",
            f"combined_final_ratio: {hot.final_ratio:.9f}",
            f"gamma2_miss_threshold_ms: {hot.filebench_threshold_ms:.6f}",
            (
                "gamma2_miss_start_bucket: "
                f"{hot.filebench_miss_start_bucket} "
                f"{bucket_label(hot.filebench_miss_start_bucket)} ms"
            ),
            f"gamma2_threshold_reason: {hot.filebench_threshold_reason}",
            "",
        ]
    )

    output_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the original Filebench backend-read ratio with the "
            "combined two-gamma hot-cache ratio."
        )
    )
    parser.add_argument(
        "--original-log",
        type=Path,
        default=DEFAULT_ORIGINAL_LOG,
        help="Original scheme Filebench lathist log",
    )
    parser.add_argument(
        "--first-gamma-log",
        type=Path,
        default=DEFAULT_FIRST_GAMMA_LOG,
        help="First gamma readopsVSreadmedfs SpinFS counter log",
    )
    parser.add_argument(
        "--second-gamma-log",
        type=Path,
        default=DEFAULT_SECOND_GAMMA_LOG,
        help="Second gamma Filebench lathist log",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("filebench_analysis/compare_original_vs_combined_gamma_ratio"),
        help="Output directory",
    )
    parser.add_argument("--op", default="rd", help="Filebench operation name")
    parser.add_argument(
        "--original-label",
        default="原始方案",
        help="Legend label for the original curve",
    )
    parser.add_argument(
        "--hot-label",
        default="热数据缓存方案",
        help="Legend label for the combined hot-cache curve",
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
        help="X-axis unit",
    )
    parser.add_argument(
        "--second-start-gap-s",
        type=float,
        default=0.0,
        help="Optional gap between gamma1 and gamma2 on the x-axis",
    )
    parser.add_argument(
        "--original-miss-threshold-ms",
        type=float,
        default=None,
        help="Manual backend latency threshold in ms for original Filebench log",
    )
    parser.add_argument(
        "--hot-miss-threshold-ms",
        type=float,
        default=None,
        help="Manual backend latency threshold in ms for the second gamma log",
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
    original_log = args.original_log.expanduser().resolve()
    first_gamma_log = args.first_gamma_log.expanduser().resolve()
    second_gamma_log = args.second_gamma_log.expanduser().resolve()
    for path in [original_log, first_gamma_log, second_gamma_log]:
        if not path.exists():
            print(f"error: log file does not exist: {path}", file=sys.stderr)
            return 2

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    original = build_original_series(
        log_path=original_log,
        label=args.original_label,
        op_name=args.op,
        manual_threshold_ms=args.original_miss_threshold_ms,
        auto_min_gap_buckets=args.auto_min_gap_buckets,
        auto_min_backend_ms=args.auto_min_backend_ms,
    )
    hot = build_combined_result(
        first_gamma_log=first_gamma_log,
        second_gamma_log=second_gamma_log,
        op_name=args.op,
        manual_threshold_ms=args.hot_miss_threshold_ms,
        auto_min_gap_buckets=args.auto_min_gap_buckets,
        auto_min_backend_ms=args.auto_min_backend_ms,
        second_start_gap_s=args.second_start_gap_s,
    )

    x_unit = (
        choose_x_unit(original, hot, args.plot_scope)
        if args.x_unit == "auto"
        else args.x_unit
    )
    plot_path = output_dir / f"original_vs_hot_cache_gamma_{args.plot_scope}.png"
    csv_path = output_dir / f"original_vs_hot_cache_gamma_{args.plot_scope}.csv"
    summary_path = output_dir / f"summary_{args.plot_scope}.txt"

    plot_compare(
        original=original,
        hot=hot,
        output_path=plot_path,
        plot_scope=args.plot_scope,
        x_unit=x_unit,
        hot_label=args.hot_label,
    )
    write_csv(original, hot, csv_path, args.plot_scope)
    write_summary(
        original=original,
        hot=hot,
        output_path=summary_path,
        plot_scope=args.plot_scope,
        x_unit=x_unit,
        original_log=original_log,
        first_gamma_log=first_gamma_log,
        second_gamma_log=second_gamma_log,
    )

    print(f"output_dir = {output_dir}")
    print(f"plot = {plot_path}")
    print(f"csv = {csv_path}")
    print(f"summary = {summary_path}")
    print(
        f"original: backend_reads_est = {original.final_backend_reads} "
        f"rd_ops = {original.final_read_ops} ratio = {original.final_ratio:.9f}"
    )
    print(
        f"hot_cache_combined: backend_reads = {hot.final_backend_reads} "
        f"read_ops = {hot.final_read_ops} ratio = {hot.final_ratio:.9f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
