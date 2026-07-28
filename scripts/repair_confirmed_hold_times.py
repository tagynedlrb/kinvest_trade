#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from kinvest_trade.repository import SqliteRepository


def _create_online_backup(db_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = (
        db_path.parent
        / f"{db_path.stem}_backup_{timestamp}_pre_confirmed_hold_time_repair.db"
    )
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)
        integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = target.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_keys:
        backup_path.unlink(missing_ok=True)
        raise RuntimeError(
            "backup verification failed: "
            f"integrity={integrity}, foreign_key_violations={len(foreign_keys)}"
        )
    return backup_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "KIS 확정 매수 원장으로 SELL_REAL 진입시각과 보유시간을 감사·교정"
        )
    )
    parser.add_argument("db_path", help="SQLite DB 파일 경로")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="온라인 백업을 만든 뒤 파생 cycle_log 필드를 교정",
    )
    parser.add_argument(
        "--tolerance-seconds",
        type=float,
        default=5.0,
        help="기록 오차로 허용할 최대 초(기본 5초)",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        parser.error(f"DB 파일 없음: {db_path}")

    backup_path: Path | None = None
    if args.apply:
        backup_path = _create_online_backup(db_path)

    repository = SqliteRepository(db_path)
    repairs = repository.repair_confirmed_cycle_entry_timing(
        apply=args.apply,
        tolerance_seconds=args.tolerance_seconds,
    )
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[확정체결 보유시간 교정] mode={mode} 대상={len(repairs)}건")
    for repair in repairs:
        print(
            "  "
            f"id={repair['cycle_log_id']} "
            f"{repair['market']} {repair['symbol']} "
            f"reason={repair['action_reason']} "
            f"entry={repair['recorded_entry_time']}"
            f"->{repair['canonical_entry_time']} "
            f"hold={repair['recorded_hold_duration_min']}"
            f"->{repair['canonical_hold_duration_min']}분"
        )
    if backup_path is not None:
        print(f"  backup={backup_path}")
        remaining = repository.repair_confirmed_cycle_entry_timing(
            apply=False,
            tolerance_seconds=args.tolerance_seconds,
        )
        print(f"  교정 후 잔여={len(remaining)}건")


if __name__ == "__main__":
    main()
