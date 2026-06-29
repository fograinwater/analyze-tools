#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${SCRIPT_DIR}/filebenchRunLog"
OUTPUT_DIR="${SCRIPT_DIR}/filebench_analysis/combined_gamma_hot_cache_ratio"
FIRST_GAMMA_BACKEND_READ_SECONDS="${FIRST_GAMMA_BACKEND_READ_SECONDS:-360}"
FIRST_GAMMA_OPEN_ARCHIVE_SCALE="${FIRST_GAMMA_OPEN_ARCHIVE_SCALE:-1}"

FIRST_GAMMA_LOG="${LOG_DIR}/readopsVSreadmedfs_log_cachefs_on_zlfs_medfs_gamma_test_20260625-200753.log"
SECOND_GAMMA_LOG="${LOG_DIR}/filebench_run_log_cachefs_on_zlfs_medfs_gamma_test_20260627-001502-gamma2.log"

"${PYTHON_BIN}" "${SCRIPT_DIR}/combine_gamma_hot_cache_ratio.py" \
  --first-gamma-log "${FIRST_GAMMA_LOG}" \
  --second-gamma-log "${SECOND_GAMMA_LOG}" \
  --first-gamma-backend-read-seconds "${FIRST_GAMMA_BACKEND_READ_SECONDS}" \
  --first-gamma-open-archive-scale "${FIRST_GAMMA_OPEN_ARCHIVE_SCALE}" \
  --output-dir "${OUTPUT_DIR}"
