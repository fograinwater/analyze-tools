#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${SCRIPT_DIR}/filebenchRunLog"
OUTPUT_DIR="${SCRIPT_DIR}/filebench_analysis/compare_original_vs_combined_gamma_ratio"

ORIGINAL_LOG="${LOG_DIR}/filebench_run_log_medfs_rand_read_30-1MB-1TB_20260625-191534.log"
FIRST_GAMMA_LOG="${LOG_DIR}/readopsVSreadmedfs_log_cachefs_on_zlfs_medfs_gamma_test_20260625-200753.log"
SECOND_GAMMA_LOG="${LOG_DIR}/filebench_run_log_cachefs_on_zlfs_medfs_gamma_test_20260627-001502-gamma2.log"

"${PYTHON_BIN}" "${SCRIPT_DIR}/compare_original_vs_combined_gamma_ratio.py" \
  --original-log "${ORIGINAL_LOG}" \
  --first-gamma-log "${FIRST_GAMMA_LOG}" \
  --second-gamma-log "${SECOND_GAMMA_LOG}" \
  --output-dir "${OUTPUT_DIR}"
