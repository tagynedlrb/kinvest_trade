from kinvest_trade.auto_trade_math import (
    DOMESTIC_COST_CALCULATION_VERSION,
    estimate_domestic_trade_costs,
    estimate_capital_gains_tax_krw,
    estimate_fx_fee_krw,
    estimate_trade_fees,
    is_domestic_sell_tax_exempt,
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


def test_domestic_stock_costs_include_sell_tax() -> None:
    estimate = estimate_domestic_trade_costs(
        entry_price=100_000,
        exit_price=101_000,
        qty=10,
        commission_rate=0.00015,
        stock_sell_tax_rate=0.002,
        product_type="KOSPI200",
    )

    assert DOMESTIC_COST_CALCULATION_VERSION == "domestic_product_tax_v2"
    assert estimate.gross_pnl_krw == 10_000.0
    assert estimate.buy_commission_krw == 150.0
    assert estimate.sell_commission_krw == 151.5
    assert estimate.sell_tax_krw == 2_020.0
    assert estimate.net_pnl_krw == 7_678.5
    assert estimate.sell_cost_krw == 2_171.5


def test_domestic_exchange_traded_products_are_sell_tax_exempt() -> None:
    for product_type in (
        "ETF",
        "ETF(실물복제/수익증권)",
        "ETN",
        "ELW",
    ):
        estimate = estimate_domestic_trade_costs(
            entry_price=100_000,
            exit_price=101_000,
            qty=10,
            commission_rate=0.00015,
            stock_sell_tax_rate=0.002,
            product_type=product_type,
        )
        assert is_domestic_sell_tax_exempt(product_type) is True
        assert estimate.sell_tax_krw == 0.0
        assert estimate.net_pnl_krw == 9_698.5

    assert is_domestic_sell_tax_exempt("KOSDAQ150") is False
