#!/usr/bin/env python3
"""Plot spinfs open-archive/read-op ratio from readopsVSreadmedfs logs."""

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


DEFAULT_LOG = Path(
    "/home/ttt/filebench-use-case/filebenchRunLog/"
    "readopsVSreadmedfs_log_cachefs_on_zlfs_medfs_gamma_test_20260625-200753.log"
)

ROW_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+"
    r"(?P<read_ops>\d+)\s+"
    r"(?P<open_archive_cnt>\d+)\s+"
    r"(?P<pinfs_read_req_total>\S+)\s+"
    r"(?P<f2fs_phys_total>\S+)\s+"
    r"(?P<f2fs_phys_read_file_sum_waf>[-+]?\d+(?:\.\d+)?)\s*$"
)


def find_cjk_font() -> Path | None:
    env_font = os.environ.get("FILEBENCH_CJK_FONT")
    candidates = []
    if env_font:
        candidates.append(Path(env_font))

    candidates.extend(
        [
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
            Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf"),
            Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            Path("/usr/share/fonts/opentype/adobe-source-han-sans/SourceHanSansCN-Regular.otf"),
            Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
            Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
        ]
    )

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


@dataclass(frozen=True)
class Sample:
    timestamp: datetime
    elapsed_s: float
    read_ops: int
    open_archive_cnt: int
    cumulative_ratio: float


@dataclass(frozen=True)
class WindowSample:
    timestamp: datetime
    elapsed_s: float
    window_seconds: float
    read_ops_delta: int
    open_archive_cnt_delta: int
    window_ratio: float


def parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")


def parse_log(path: Path) -> list[Sample]:
    samples: list[Sample] = []
    first_ts: datetime | None = None

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("timestamp"):
                continue

            match = ROW_RE.match(line)
            if not match:
                raise ValueError(f"line {line_number}: cannot parse row: {line}")

            timestamp = parse_timestamp(match.group("timestamp"))
            if first_ts is None:
                first_ts = timestamp

            read_ops = int(match.group("read_ops"))
            open_archive_cnt = int(match.group("open_archive_cnt"))
            elapsed_s = (timestamp - first_ts).total_seconds()
            ratio = open_archive_cnt / read_ops if read_ops else math.nan
            samples.append(
                Sample(
                    timestamp=timestamp,
                    elapsed_s=elapsed_s,
                    read_ops=read_ops,
                    open_archive_cnt=open_archive_cnt,
                    cumulative_ratio=ratio,
                )
            )

    if not samples:
        raise ValueError(f"no samples were found in {path}")

    return samples


def compute_window_samples(samples: list[Sample]) -> list[WindowSample]:
    windows: list[WindowSample] = []
    for prev, cur in zip(samples, samples[1:]):
        window_seconds = cur.elapsed_s - prev.elapsed_s
        read_ops_delta = cur.read_ops - prev.read_ops
        open_archive_cnt_delta = cur.open_archive_cnt - prev.open_archive_cnt
        ratio = (
            open_archive_cnt_delta / read_ops_delta
            if read_ops_delta > 0
            else math.nan
        )
        windows.append(
            WindowSample(
                timestamp=cur.timestamp,
                elapsed_s=cur.elapsed_s,
                window_seconds=window_seconds,
                read_ops_delta=read_ops_delta,
                open_archive_cnt_delta=open_archive_cnt_delta,
                window_ratio=ratio,
            )
        )
    return windows


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


def plot_cumulative_ratio(samples: list[Sample], output_path: Path) -> None:
    plt = configure_matplotlib()
    fig, ax = plt.subplots(figsize=(10, 5.8))

    elapsed_min = [sample.elapsed_s / 60.0 for sample in samples]
    ratios = [sample.cumulative_ratio for sample in samples]

    ax.plot(elapsed_min, ratios, marker="o", linewidth=2.0, markersize=4.5)
    ax.set_title("读磁电盘次数 / 读操作数 随时间变化")
    ax.set_xlabel("从首次采样开始的运行时间（分钟）")
    ax.set_ylabel("累计读磁电盘次数 / 累计读操作数")
    ax.grid(True, linestyle="--", linewidth=0.6, alpha=0.35)

    final = samples[-1]
    ax.text(
        0.98,
        0.94,
        f"最终比例：{final.cumulative_ratio:.6f}",
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=13,
        bbox={"boxstyle": "round,pad=0.28", "facecolor": "white", "alpha": 0.8},
    )

    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)


def write_sample_csv(samples: list[Sample], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp",
                "elapsed_s",
                "elapsed_min",
                "spinfs_read_ops",
                "spinfs_open_archive_cnt",
                "cumulative_ratio",
            ]
        )
        for sample in samples:
            writer.writerow(
                [
                    sample.timestamp.isoformat(sep=" "),
                    f"{sample.elapsed_s:.3f}",
                    f"{sample.elapsed_s / 60.0:.6f}",
                    sample.read_ops,
                    sample.open_archive_cnt,
                    f"{sample.cumulative_ratio:.9f}",
                ]
            )


def write_window_csv(windows: list[WindowSample], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "timestamp",
                "elapsed_s",
                "elapsed_min",
                "window_seconds",
                "spinfs_read_ops_delta",
                "spinfs_open_archive_cnt_delta",
                "window_ratio",
            ]
        )
        for window in windows:
            writer.writerow(
                [
                    window.timestamp.isoformat(sep=" "),
                    f"{window.elapsed_s:.3f}",
                    f"{window.elapsed_s / 60.0:.6f}",
                    f"{window.window_seconds:.3f}",
                    window.read_ops_delta,
                    window.open_archive_cnt_delta,
                    f"{window.window_ratio:.9f}",
                ]
            )


def write_summary(
    samples: list[Sample], windows: list[WindowSample], output_path: Path, log_path: Path
) -> None:
    first = samples[0]
    final = samples[-1]
    valid_window_ratios = [
        window.window_ratio for window in windows if not math.isnan(window.window_ratio)
    ]
    avg_window_ratio = (
        sum(valid_window_ratios) / len(valid_window_ratios)
        if valid_window_ratios
        else math.nan
    )

    lines = [
        "SpinFS read backend ratio analysis",
        "=" * 38,
        "",
        f"log_path: {log_path}",
        f"samples: {len(samples)}",
        f"start_time: {first.timestamp.isoformat(sep=' ')}",
        f"end_time: {final.timestamp.isoformat(sep=' ')}",
        f"elapsed_s: {final.elapsed_s:.3f}",
        f"elapsed_min: {final.elapsed_s / 60.0:.6f}",
        f"final_spinfs_read_ops: {final.read_ops}",
        f"final_spinfs_open_archive_cnt: {final.open_archive_cnt}",
        f"final_cumulative_ratio: {final.cumulative_ratio:.9f}",
        f"avg_window_ratio: {avg_window_ratio:.9f}",
        "",
    ]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plot spinfs_open_archive_cnt / spinfs_read_ops over time from "
            "readopsVSreadmedfs logs."
        )
    )
    parser.add_argument(
        "log",
        nargs="?",
        type=Path,
        default=DEFAULT_LOG,
        help="Input readopsVSreadmedfs log",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory. Defaults to filebench_analysis/<log>_spinfs_read_ratio_analysis",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    log_path = args.log.expanduser().resolve()
    if not log_path.exists():
        print(f"error: log file does not exist: {log_path}", file=sys.stderr)
        return 2

    if args.output_dir is None:
        output_dir = (
            Path("/home/ttt/filebench-use-case/filebench_analysis")
            / f"{log_path.stem}_spinfs_read_ratio_analysis"
        )
    else:
        output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    samples = parse_log(log_path)
    windows = compute_window_samples(samples)

    plot_path = output_dir / "spinfs_open_archive_ratio_over_time.png"
    sample_csv_path = output_dir / "spinfs_open_archive_ratio_timeseries.csv"
    window_csv_path = output_dir / "spinfs_open_archive_ratio_windows.csv"
    summary_path = output_dir / "summary.txt"

    plot_cumulative_ratio(samples, plot_path)
    write_sample_csv(samples, sample_csv_path)
    write_window_csv(windows, window_csv_path)
    write_summary(samples, windows, summary_path, log_path)

    final = samples[-1]
    print(f"output_dir = {output_dir}")
    print(f"plot = {plot_path}")
    print(f"timeseries_csv = {sample_csv_path}")
    print(f"window_csv = {window_csv_path}")
    print(f"summary = {summary_path}")
    print(f"samples = {len(samples)}")
    print(f"final_spinfs_read_ops = {final.read_ops}")
    print(f"final_spinfs_open_archive_cnt = {final.open_archive_cnt}")
    print(f"final_cumulative_ratio = {final.cumulative_ratio:.9f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
