#!/usr/bin/env python3
"""Compare stable-state Filebench read throughput from two logs."""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


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
    ops: int


@dataclass(frozen=True)
class ParsedLog:
    snapshots: list[Snapshot]
    origin_time_s: float


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

            snapshots.append(
                Snapshot(
                    time_s=current_time,
                    ops=int(op_match.group("ops")),
                )
            )

    if not snapshots:
        raise ValueError(f"No operation snapshots named {op_name!r} were found in {path}")

    origin_time = running_time if running_time is not None else snapshots[0].time_s
    return ParsedLog(snapshots=snapshots, origin_time_s=origin_time)


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
class StableThroughput:
    label: str
    log_path: Path
    stable_start_h: float
    stable_end_h: float
    stable_seconds: float
    stable_ops: float
    ops_per_second: float
    total_reads: int
    total_elapsed_h: float


@dataclass(frozen=True)
class StableIntervalThroughput:
    label: str
    window_index: int
    start_elapsed_s: float
    end_elapsed_s: float
    start_elapsed_h: float
    end_elapsed_h: float
    window_seconds: float
    rd_ops_delta: int
    rd_ops_per_second: float
    rd_ops_per_30s: float


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

    import matplotlib.pyplot as plt

    return plt


def choose_stable_start_s(
    points: list[tuple[float, int]],
    origin_time_s: float,
    stable_tail_ratio: float,
    manual_start_h: float | None,
) -> float:
    if manual_start_h is not None:
        return origin_time_s + manual_start_h * 3600.0

    positive_points = [(time_s, ops) for time_s, ops in points if ops > 0]
    if not positive_points:
        return origin_time_s

    first_positive_time = positive_points[0][0]
    last_time = positive_points[-1][0]
    return first_positive_time + (last_time - first_positive_time) * (
        1.0 - stable_tail_ratio
    )


def compute_stable_throughput(
    log_path: Path,
    label: str,
    op_name: str,
    stable_tail_ratio: float,
    manual_start_h: float | None,
) -> StableThroughput:
    parsed = parse_log(log_path, op_name)
    points = [(parsed.origin_time_s, 0)] + [
        (snapshot.time_s, snapshot.ops) for snapshot in parsed.snapshots
    ]
    final_time_s = points[-1][0]
    final_ops = points[-1][1]
    stable_start_s = choose_stable_start_s(
        points, parsed.origin_time_s, stable_tail_ratio, manual_start_h
    )
    stable_start_s = min(max(stable_start_s, parsed.origin_time_s), final_time_s)

    stable_ops = 0.0
    stable_seconds = 0.0

    for (start_s, start_ops), (end_s, end_ops) in zip(points, points[1:]):
        if end_s <= stable_start_s or end_s <= start_s:
            continue
        interval_start = max(start_s, stable_start_s)
        interval_end = end_s
        interval_seconds = interval_end - interval_start
        interval_total_seconds = end_s - start_s
        if interval_seconds <= 0:
            continue

        stable_seconds += interval_seconds
        # If stable_start_s splits a 30s report window, proportionally assign
        # that window's completed operations. The maximum approximation error is
        # bounded to one Filebench report interval.
        stable_ops += (end_ops - start_ops) * (
            interval_seconds / interval_total_seconds
        )

    ops_per_second = stable_ops / stable_seconds if stable_seconds else math.nan
    stable_start_h = (stable_start_s - parsed.origin_time_s) / 3600.0
    stable_end_h = (final_time_s - parsed.origin_time_s) / 3600.0

    return StableThroughput(
        label=label,
        log_path=log_path,
        stable_start_h=stable_start_h,
        stable_end_h=stable_end_h,
        stable_seconds=stable_seconds,
        stable_ops=stable_ops,
        ops_per_second=ops_per_second,
        total_reads=final_ops,
        total_elapsed_h=stable_end_h,
    )


def compute_stable_intervals(
    log_path: Path,
    label: str,
    op_name: str,
    stable_tail_ratio: float,
    manual_start_h: float | None,
) -> list[StableIntervalThroughput]:
    parsed = parse_log(log_path, op_name)
    points = [(parsed.origin_time_s, 0)] + [
        (snapshot.time_s, snapshot.ops) for snapshot in parsed.snapshots
    ]
    stable_start_s = choose_stable_start_s(
        points, parsed.origin_time_s, stable_tail_ratio, manual_start_h
    )

    intervals: list[StableIntervalThroughput] = []
    for (start_s, start_ops), (end_s, end_ops) in zip(points, points[1:]):
        if start_s < stable_start_s or end_s <= start_s:
            continue

        window_seconds = end_s - start_s
        rd_ops_delta = end_ops - start_ops
        rd_ops_per_second = rd_ops_delta / window_seconds
        intervals.append(
            StableIntervalThroughput(
                label=label,
                window_index=len(intervals) + 1,
                start_elapsed_s=start_s - parsed.origin_time_s,
                end_elapsed_s=end_s - parsed.origin_time_s,
                start_elapsed_h=(start_s - parsed.origin_time_s) / 3600.0,
                end_elapsed_h=(end_s - parsed.origin_time_s) / 3600.0,
                window_seconds=window_seconds,
                rd_ops_delta=rd_ops_delta,
                rd_ops_per_second=rd_ops_per_second,
                rd_ops_per_30s=rd_ops_per_second * 30.0,
            )
        )

    return intervals


def plot_bar(results: list[StableThroughput], output_path: Path, log_y: bool) -> None:
    plt = configure_matplotlib()
    fig, ax = plt.subplots(figsize=(9, 5.5))

    colors = ["#2563eb", "#dc2626", "#059669", "#7c3aed"]
    labels = [result.label for result in results]
    values = [result.ops_per_second for result in results]
    bars = ax.bar(labels, values, color=colors[: len(results)], width=0.55)

    ax.set_title("稳定状态单位时间完成操作数对比")
    ax.set_ylabel("稳定阶段平均吞吐（rd ops/s）")
    if log_y:
        ax.set_yscale("log")
        ax.set_ylabel("稳定阶段平均吞吐（rd ops/s，对数坐标）")
    ax.grid(True, axis="y", linestyle="--", linewidth=0.6, alpha=0.35)

    for bar, value in zip(bars, values):
        if math.isnan(value):
            label = "nan"
            text_y = 0
        else:
            label = f"{value:.6f}"
            text_y = value
        ax.annotate(
            label,
            xy=(bar.get_x() + bar.get_width() / 2.0, text_y),
            xytext=(0, 5),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=12,
        )

    if not log_y:
        max_value = max(value for value in values if not math.isnan(value))
        ax.set_ylim(0, max_value * 1.18 if max_value > 0 else 1)

    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def plot_30s_throughput_bars(
    interval_rows: list[StableIntervalThroughput], output_path: Path
) -> None:
    plt = configure_matplotlib()

    rows_by_label: dict[str, list[StableIntervalThroughput]] = {}
    for row in interval_rows:
        rows_by_label.setdefault(row.label, []).append(row)

    if not rows_by_label:
        return

    labels = list(rows_by_label)
    fig, axes = plt.subplots(
        len(labels),
        1,
        figsize=(13, 4.2 * len(labels)),
        sharey=True,
    )
    if len(labels) == 1:
        axes = [axes]

    colors = ["#2563eb", "#dc2626", "#059669", "#7c3aed"]
    max_value = max(row.rd_ops_per_30s for row in interval_rows)

    for ax, label, color in zip(axes, labels, colors):
        rows = rows_by_label[label]
        x_values = [row.window_index for row in rows]
        y_values = [row.rd_ops_per_30s for row in rows]
        avg_value = sum(y_values) / len(y_values) if y_values else math.nan

        ax.bar(
            x_values,
            y_values,
            width=0.92,
            color=color,
            alpha=0.82,
            linewidth=0,
        )
        if not math.isnan(avg_value):
            ax.axhline(
                avg_value,
                color="#111827",
                linestyle="--",
                linewidth=1.1,
                label=f"平均值：{avg_value:.2f}",
            )
            ax.legend(loc="upper right")

        ax.set_title(f"{label}：稳定阶段每 30s 吞吐量")
        ax.set_ylabel("读操作数 / 30s")
        ax.set_xlim(0.5, len(rows) + 0.5)
        ax.grid(True, axis="y", linestyle="--", linewidth=0.6, alpha=0.35)
        ax.text(
            0.01,
            0.91,
            f"{len(rows)} 个窗口",
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=12,
        )

    if max_value > 0:
        axes[0].set_ylim(0, max_value * 1.12)
    axes[-1].set_xlabel("稳定阶段第 N 个 30s 窗口")
    fig.suptitle("稳定状态每 30s 吞吐量柱状图对比", fontsize=18)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def write_summary(results: list[StableThroughput], output_path: Path) -> None:
    lines = [
        "Filebench stable-state throughput comparison",
        "=" * 47,
        "",
    ]

    for result in results:
        lines.extend(
            [
                f"[{result.label}]",
                f"log_path: {result.log_path}",
                f"stable_start_h: {result.stable_start_h:.6f}",
                f"stable_end_h: {result.stable_end_h:.6f}",
                f"stable_seconds: {result.stable_seconds:.3f}",
                f"stable_rd_ops: {result.stable_ops:.3f}",
                f"stable_rd_ops_per_second: {result.ops_per_second:.9f}",
                f"total_reads: {result.total_reads}",
                f"total_elapsed_h: {result.total_elapsed_h:.6f}",
                "",
            ]
        )

    if len(results) == 2:
        base, cached = results
        speedup = (
            cached.ops_per_second / base.ops_per_second
            if base.ops_per_second and not math.isnan(base.ops_per_second)
            else math.nan
        )
        lines.extend(
            [
                "[对比]",
                f"speedup_second_vs_first: {speedup:.6f}",
                "",
            ]
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")


def write_csv(results: list[StableThroughput], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "label",
                "stable_start_h",
                "stable_end_h",
                "stable_seconds",
                "stable_rd_ops",
                "stable_rd_ops_per_second",
                "total_reads",
                "total_elapsed_h",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    result.label,
                    f"{result.stable_start_h:.9f}",
                    f"{result.stable_end_h:.9f}",
                    f"{result.stable_seconds:.3f}",
                    f"{result.stable_ops:.3f}",
                    f"{result.ops_per_second:.9f}",
                    result.total_reads,
                    f"{result.total_elapsed_h:.9f}",
                ]
            )


def write_interval_csv(
    interval_rows: list[StableIntervalThroughput], output_path: Path
) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "label",
                "window_index",
                "start_elapsed_s",
                "end_elapsed_s",
                "start_elapsed_h",
                "end_elapsed_h",
                "window_seconds",
                "rd_ops_delta",
                "rd_ops_per_second",
                "rd_ops_per_30s",
            ]
        )
        for row in interval_rows:
            writer.writerow(
                [
                    row.label,
                    row.window_index,
                    f"{row.start_elapsed_s:.3f}",
                    f"{row.end_elapsed_s:.3f}",
                    f"{row.start_elapsed_h:.9f}",
                    f"{row.end_elapsed_h:.9f}",
                    f"{row.window_seconds:.3f}",
                    row.rd_ops_delta,
                    f"{row.rd_ops_per_second:.9f}",
                    f"{row.rd_ops_per_30s:.6f}",
                ]
            )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Draw a bar chart comparing stable-state rd ops/s from two "
            "Filebench logs."
        )
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
        help="Bar labels for the two logs",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("filebench_analysis/compare_stable_ops"),
        help="Output directory",
    )
    parser.add_argument(
        "--op",
        default="rd",
        help="Filebench operation name to analyze (default: rd)",
    )
    parser.add_argument(
        "--stable-tail-ratio",
        type=float,
        default=0.5,
        help=(
            "Stable-state window as the tail ratio of each run after first "
            "nonzero ops report (default: 0.5, i.e. last half)"
        ),
    )
    parser.add_argument(
        "--stable-start-h",
        nargs="*",
        type=float,
        default=None,
        help=(
            "Manual stable-state start hour from Filebench Running. Provide "
            "one value for both logs or two values, one per log."
        ),
    )
    parser.add_argument(
        "--log-y",
        action="store_true",
        help="Use a logarithmic y-axis for the bar chart",
    )
    return parser.parse_args(argv)


def parse_manual_starts(values: list[float] | None) -> list[float | None]:
    if values is None or len(values) == 0:
        return [None, None]
    if len(values) == 1:
        return [values[0], values[0]]
    if len(values) == 2:
        return [values[0], values[1]]
    raise ValueError("--stable-start-h accepts zero, one, or two values")


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if not 0 < args.stable_tail_ratio <= 1:
        print("error: --stable-tail-ratio must be in (0, 1]", file=sys.stderr)
        return 2

    if not args.logs:
        log_paths = DEFAULT_LOGS
    elif len(args.logs) == 2:
        log_paths = args.logs
    else:
        print("error: provide exactly two logs, or omit logs to use defaults", file=sys.stderr)
        return 2

    try:
        manual_starts = parse_manual_starts(args.stable_start_h)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    log_paths = [path.expanduser().resolve() for path in log_paths]
    for path in log_paths:
        if not path.exists():
            print(f"error: log file does not exist: {path}", file=sys.stderr)
            return 2

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    results = [
        compute_stable_throughput(
            log_path=log_path,
            label=label,
            op_name=args.op,
            stable_tail_ratio=args.stable_tail_ratio,
            manual_start_h=manual_start,
        )
        for log_path, label, manual_start in zip(log_paths, args.labels, manual_starts)
    ]
    interval_rows = [
        row
        for log_path, label, manual_start in zip(log_paths, args.labels, manual_starts)
        for row in compute_stable_intervals(
            log_path=log_path,
            label=label,
            op_name=args.op,
            stable_tail_ratio=args.stable_tail_ratio,
            manual_start_h=manual_start,
        )
    ]

    plot_path = output_dir / "stable_rd_ops_per_second_bar.png"
    interval_plot_path = output_dir / "stable_30s_throughput_bar_compare.png"
    summary_path = output_dir / "stable_ops_summary.txt"
    csv_path = output_dir / "stable_ops_summary.csv"
    interval_csv_path = output_dir / "stable_30s_throughput.csv"

    plot_bar(results, plot_path, args.log_y)
    plot_30s_throughput_bars(interval_rows, interval_plot_path)
    write_summary(results, summary_path)
    write_csv(results, csv_path)
    write_interval_csv(interval_rows, interval_csv_path)

    print(f"output_dir = {output_dir}")
    print(f"plot = {plot_path}")
    print(f"stable_30s_throughput_plot = {interval_plot_path}")
    print(f"summary = {summary_path}")
    print(f"csv = {csv_path}")
    print(f"stable_30s_throughput_csv = {interval_csv_path}")
    for result in results:
        print(
            f"{result.label}: stable_start_h = {result.stable_start_h:.6f} "
            f"stable_rd_ops = {result.stable_ops:.3f} "
            f"stable_seconds = {result.stable_seconds:.3f} "
            f"ops_per_second = {result.ops_per_second:.9f}"
        )

    if len(results) == 2 and results[0].ops_per_second:
        print(
            "speedup_second_vs_first = "
            f"{results[1].ops_per_second / results[0].ops_per_second:.6f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
