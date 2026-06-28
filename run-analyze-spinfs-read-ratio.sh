#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_DIR="${SCRIPT_DIR}/filebenchRunLog"

SPINFS_LOG="${LOG_DIR}/readopsVSreadmedfs_log_cachefs_on_zlfs_medfs_gamma_test_20260625-200753.log"

"${PYTHON_BIN}" "${SCRIPT_DIR}/analyze_spinfs_read_ratio.py" "${SPINFS_LOG}"
