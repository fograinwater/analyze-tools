#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${SCRIPT_DIR}/filebenchRunLog"
OUTPUT_DIR="${SCRIPT_DIR}/filebench_analysis/compare_stable_ops"

ORIGINAL_LOG="${LOG_DIR}/filebench_run_log_medfs_rand_read_30-1MB-1TB_20260625-191534.log"
HOT_CACHE_LOG="${LOG_DIR}/filebench_run_log_cachefs_on_zlfs_medfs_rand_read_30-64KB-1TB_20260120-123004.log"

"${PYTHON_BIN}" "${SCRIPT_DIR}/compare_filebench_stable_ops.py" \
  "${ORIGINAL_LOG}" \
  "${HOT_CACHE_LOG}" \
  --output-dir "${OUTPUT_DIR}"
