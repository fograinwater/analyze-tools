#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${SCRIPT_DIR}/filebenchRunLog"

HOT167G_LOG="${LOG_DIR}/filebench_run_log_medfs_rand_read_30-1MB-1TB-hot167G_20260625-011858.log"
ORIGINAL_LOG="${LOG_DIR}/filebench_run_log_medfs_rand_read_30-1MB-1TB_20260625-191534.log"
HOT_CACHE_LOG="${LOG_DIR}/filebench_run_log_cachefs_on_zlfs_medfs_rand_read_30-64KB-1TB_20260120-123004.log"

"${PYTHON_BIN}" "${SCRIPT_DIR}/analyze_filebench_latency.py" "${HOT167G_LOG}"
"${PYTHON_BIN}" "${SCRIPT_DIR}/analyze_filebench_latency.py" "${ORIGINAL_LOG}"
"${PYTHON_BIN}" "${SCRIPT_DIR}/analyze_filebench_latency.py" "${HOT_CACHE_LOG}"
