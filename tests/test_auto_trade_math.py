from kinvest_trade.auto_trade_math import (
    estimate_capital_gains_tax_krw,
    estimate_fx_fee_krw,
    estimate_trade_fees,
)


def test_estimate_trade_fees_for_buy() -> None:
    fees = estimate_trade_fees("buy", qty=2, price=25.0, commission_rate=0.0025, sec_fee_rate=0.0000206)
    assert fees.notional_usd == 50.0
    assert fees.commission_usd == 0.125
    assert fees.sec_fee_usd == 0.0


def test_estimate_trade_fees_for_sell_includes_sec() -> None:
    fees = estimate_trade_fees("sell", qty=2, price=25.0, commission_rate=0.0025, sec_fee_rate=0.0000206)
    assert round(fees.total_fees_usd, 6) == round(0.125 + (50.0 * 0.0000206), 6)


def test_estimate_fx_fee_krw() -> None:
    assert estimate_fx_fee_krw(100.0, 1300.0, 0.001) == 130.0


def test_estimate_capital_gains_tax_krw() -> None:
    assert estimate_capital_gains_tax_krw(2_000_000, 2_500_000, 0.22) == 0.0
    assert estimate_capital_gains_tax_krw(3_500_000, 2_500_000, 0.22) == 220_000.0
