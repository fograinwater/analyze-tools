#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${SCRIPT_DIR}/filebenchRunLog"
OUTPUT_DIR="${SCRIPT_DIR}/filebench_analysis/compare_filebench_spinfs_read_ratio"

FILEBENCH_LOG="${LOG_DIR}/filebench_run_log_cachefs_on_zlfs_medfs_rand_read_30-64KB-1TB_20260120-123004.log"
SPINFS_LOG="${LOG_DIR}/readopsVSreadmedfs_log_cachefs_on_zlfs_medfs_gamma_test_20260625-200753.log"

"${PYTHON_BIN}" "${SCRIPT_DIR}/compare_filebench_spinfs_read_ratio.py" \
  --filebench-log "${FILEBENCH_LOG}" \
  --spinfs-log "${SPINFS_LOG}" \
  --output-dir "${OUTPUT_DIR}"
