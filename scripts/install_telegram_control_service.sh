#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/ubuntu/kinvest_trade"
USER_UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_NAME="kinvest-telegram-control.service"

"${PROJECT_ROOT}/scripts/bootstrap_runtime_env.sh"
mkdir -p "${PROJECT_ROOT}/data" "${PROJECT_ROOT}/logs" "${PROJECT_ROOT}/state"
for private_path in keys data logs state; do
    if [[ -e "${PROJECT_ROOT}/${private_path}" ]]; then
        chmod -R go-rwx "${PROJECT_ROOT}/${private_path}"
    fi
done
mkdir -p "${USER_UNIT_DIR}"
cp "${PROJECT_ROOT}/systemd/${UNIT_NAME}" "${USER_UNIT_DIR}/${UNIT_NAME}"
systemctl --user daemon-reload
systemctl --user enable --now "${UNIT_NAME}"
systemctl --user status --no-pager "${UNIT_NAME}"
