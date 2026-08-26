#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/ubuntu/kinvest_trade}"
DATA_DIR="${PROJECT_ROOT}/data"
OLDER_THAN_DAYS=14
APPLY=false

usage() {
    printf '%s\n' \
        "Usage: $0 [--apply] [--older-than-days N] [--data-dir PATH]" \
        "" \
        "Dry-run by default. With --apply, verified SQLite backups are" \
        "compressed one at a time with zstd before the source is removed."
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply)
            APPLY=true
            shift
            ;;
        --older-than-days)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            OLDER_THAN_DAYS="$2"
            shift 2
            ;;
        --data-dir)
            [[ $# -ge 2 ]] || { usage >&2; exit 2; }
            DATA_DIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ ! "$OLDER_THAN_DAYS" =~ ^[0-9]+$ ]]; then
    printf 'older-than-days must be a non-negative integer\n' >&2
    exit 2
fi
if [[ ! -d "$DATA_DIR" ]]; then
    printf 'data directory not found: %s\n' "$DATA_DIR" >&2
    exit 2
fi
for command_name in sqlite3 zstd sha256sum; do
    command -v "$command_name" >/dev/null || {
        printf 'required command not found: %s\n' "$command_name" >&2
        exit 2
    }
done

umask 077
candidate_count=0
source_bytes=0
archive_bytes=0

while IFS= read -r -d '' source_path; do
    candidate_count=$((candidate_count + 1))
    source_size=$(stat -c '%s' "$source_path")
    source_bytes=$((source_bytes + source_size))
    archive_path="${source_path}.zst"
    checksum_path="${archive_path}.sha256"

    if [[ "$APPLY" != true ]]; then
        printf 'would_archive size=%s path=%s\n' "$source_size" "$source_path"
        continue
    fi
    if [[ -e "$archive_path" || -e "$checksum_path" ]]; then
        printf 'archive target already exists: %s\n' "$archive_path" >&2
        exit 1
    fi

    integrity=$(sqlite3 -readonly -cmd '.timeout 30000' "$source_path" \
        'PRAGMA quick_check;')
    if [[ "$integrity" != "ok" ]]; then
        printf 'backup integrity check failed: %s (%s)\n' \
            "$source_path" "$integrity" >&2
        exit 1
    fi

    archive_tmp="${archive_path}.tmp.$$"
    checksum_tmp="${checksum_path}.tmp.$$"
    cleanup() {
        rm -f -- "$archive_tmp" "$checksum_tmp"
    }
    trap cleanup EXIT

    if command -v ionice >/dev/null; then
        ionice -c 3 nice -n 10 zstd -q -T1 --check \
            "$source_path" -o "$archive_tmp"
    else
        nice -n 10 zstd -q -T1 --check "$source_path" -o "$archive_tmp"
    fi
    zstd -q -t "$archive_tmp"
    chmod 600 "$archive_tmp"
    mv -- "$archive_tmp" "$archive_path"
    digest=$(sha256sum "$archive_path" | awk '{print $1}')
    printf '%s  %s\n' "$digest" "$(basename "$archive_path")" \
        > "$checksum_tmp"
    chmod 600 "$checksum_tmp"
    mv -- "$checksum_tmp" "$checksum_path"
    rm -- "$source_path"
    trap - EXIT

    compressed_size=$(stat -c '%s' "$archive_path")
    archive_bytes=$((archive_bytes + compressed_size))
    printf 'archived source_bytes=%s archive_bytes=%s path=%s\n' \
        "$source_size" "$compressed_size" "$archive_path"
done < <(
    find "$DATA_DIR" -maxdepth 1 -type f \
        -name 'trading_backup_*.db' \
        -mtime "+${OLDER_THAN_DAYS}" \
        -print0 | sort -z
)

if [[ "$APPLY" == true ]]; then
    reclaimed_bytes=$((source_bytes - archive_bytes))
    printf 'summary archived=%s source_bytes=%s archive_bytes=%s reclaimed_bytes=%s\n' \
        "$candidate_count" "$source_bytes" "$archive_bytes" "$reclaimed_bytes"
else
    printf 'summary dry_run=true candidates=%s source_bytes=%s\n' \
        "$candidate_count" "$source_bytes"
fi
