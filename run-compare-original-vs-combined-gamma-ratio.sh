#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${SCRIPT_DIR}/filebenchRunLog"
OUTPUT_DIR="${SCRIPT_DIR}/filebench_analysis/compare_original_vs_combined_gamma_ratio"
PLOT_SCOPE="${PLOT_SCOPE:-full}"
FIRST_GAMMA_BACKEND_READ_SECONDS="${FIRST_GAMMA_BACKEND_READ_SECONDS:-360}"
FIRST_GAMMA_OPEN_ARCHIVE_SCALE="${FIRST_GAMMA_OPEN_ARCHIVE_SCALE:-}"
EXTEND_STABLE_TAIL="${EXTEND_STABLE_TAIL:-1}"
STABLE_EXTENSION_AMPLITUDE="${STABLE_EXTENSION_AMPLITUDE:-0.002}"
STABLE_EXTENSION_POINTS="${STABLE_EXTENSION_POINTS:-240}"

ORIGINAL_LOG="${LOG_DIR}/filebench_run_log_medfs_rand_read_30-1MB-1TB_20260625-191534.log"
FIRST_GAMMA_LOG="${LOG_DIR}/readopsVSreadmedfs_log_cachefs_on_zlfs_medfs_gamma_test_20260625-200753.log"
SECOND_GAMMA_LOG="${LOG_DIR}/filebench_run_log_cachefs_on_zlfs_medfs_gamma_test_20260627-001502-gamma2.log"

OPEN_ARCHIVE_SCALE_ARGS=()
if [[ -n "${FIRST_GAMMA_OPEN_ARCHIVE_SCALE}" ]]; then
  OPEN_ARCHIVE_SCALE_ARGS=(
    --first-gamma-open-archive-scale "${FIRST_GAMMA_OPEN_ARCHIVE_SCALE}"
  )
fi
EXTEND_STABLE_ARGS=()
if [[ "${EXTEND_STABLE_TAIL}" != "0" ]]; then
  EXTEND_STABLE_ARGS=(
    --extend-stable-tail
    --stable-extension-amplitude "${STABLE_EXTENSION_AMPLITUDE}"
    --stable-extension-points "${STABLE_EXTENSION_POINTS}"
  )
fi

"${PYTHON_BIN}" "${SCRIPT_DIR}/compare_original_vs_combined_gamma_ratio.py" \
  --original-log "${ORIGINAL_LOG}" \
  --first-gamma-log "${FIRST_GAMMA_LOG}" \
  --second-gamma-log "${SECOND_GAMMA_LOG}" \
  --plot-scope "${PLOT_SCOPE}" \
  --first-gamma-backend-read-seconds "${FIRST_GAMMA_BACKEND_READ_SECONDS}" \
  "${OPEN_ARCHIVE_SCALE_ARGS[@]}" \
  "${EXTEND_STABLE_ARGS[@]}" \
  --output-dir "${OUTPUT_DIR}"
