#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/ubuntu/kinvest_trade"
VENV_DIR="${PROJECT_ROOT}/.venv"
LOCK_FILE="${PROJECT_ROOT}/requirements-runtime.lock"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    python3 -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install --requirement "${LOCK_FILE}"
"${VENV_DIR}/bin/python" -m pip install --no-deps --editable "${PROJECT_ROOT}"
"${VENV_DIR}/bin/python" -m pip check
"${VENV_DIR}/bin/python" -c \
    "import exchange_calendars, httpx, kinvest_trade, tradingview_screener"
