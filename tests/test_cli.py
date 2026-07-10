from __future__ import annotations

from types import SimpleNamespace

from kinvest_trade.cli import get_order_submission_status


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
