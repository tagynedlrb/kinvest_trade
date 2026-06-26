#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/ubuntu/kinvest_trade"
USER_UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_NAME="kinvest-telegram-control.service"

mkdir -p "${USER_UNIT_DIR}"
cp "${PROJECT_ROOT}/systemd/${UNIT_NAME}" "${USER_UNIT_DIR}/${UNIT_NAME}"
systemctl --user daemon-reload
systemctl --user enable --now "${UNIT_NAME}"
systemctl --user status --no-pager "${UNIT_NAME}"
