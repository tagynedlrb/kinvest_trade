from __future__ import annotations

import logging
from types import SimpleNamespace

from kinvest_trade.cli import _configure_logging, get_order_submission_status


def _config(*, env: str, dry_run: bool, live_trading_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        credentials=SimpleNamespace(
            env=env,
            dry_run=dry_run,
            live_trading_enabled=live_trading_enabled,
        )
    )


def test_order_submission_status_allows_paper_when_dry_run_is_false() -> None:
    status = get_order_submission_status(
        _config(env="vps", dry_run=False, live_trading_enabled=False)
    )

    assert status == {
        "paper_order_submission": "enabled",
        "prod_order_submission": "not_applicable_paper_env",
        "live_guard_scope": "prod_only",
    }


def test_order_submission_status_blocks_paper_only_by_dry_run() -> None:
    status = get_order_submission_status(
        _config(env="vps", dry_run=True, live_trading_enabled=True)
    )

    assert status["paper_order_submission"] == "blocked_by_dry_run"
    assert status["prod_order_submission"] == "not_applicable_paper_env"
    assert status["live_guard_scope"] == "prod_only"


def test_order_submission_status_blocks_prod_by_live_guard() -> None:
    status = get_order_submission_status(
        _config(env="prod", dry_run=False, live_trading_enabled=False)
    )

    assert status["paper_order_submission"] == "not_applicable_prod_env"
    assert status["prod_order_submission"] == "blocked_by_live_guard"
    assert status["live_guard_scope"] == "prod_only"


def test_order_submission_status_allows_prod_when_all_guards_open() -> None:
    status = get_order_submission_status(
        _config(env="prod", dry_run=False, live_trading_enabled=True)
    )

    assert status["paper_order_submission"] == "not_applicable_prod_env"
    assert status["prod_order_submission"] == "enabled"
    assert status["live_guard_scope"] == "prod_only"


def test_info_logging_keeps_network_clients_above_sensitive_request_level(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KINVEST_LOG_LEVEL", "INFO")
    logger_names = ("httpx", "httpcore", "urllib3")
    original_levels = {
        name: logging.getLogger(name).level for name in logger_names
    }
    try:
        _configure_logging()

        assert all(
            logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
            for name in logger_names
        )
    finally:
        for name, level in original_levels.items():
            logging.getLogger(name).setLevel(level)
