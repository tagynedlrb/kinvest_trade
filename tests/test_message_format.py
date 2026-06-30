from kinvest_trade.message_format import (
    format_krw,
    format_market_korean,
    format_pct,
    format_reason_korean,
    format_side_korean,
    format_usd,
)


def test_format_krw_uses_signed_korean_currency() -> None:
    assert format_krw(4000) == "+4,000원"
    assert format_krw(-2500) == "-2,500원"


def test_format_usd_uses_signed_dollar_currency() -> None:
    assert format_usd(4.5) == "+$4.50"
    assert format_usd(-1.25) == "-$1.25"


def test_format_pct_adds_sign() -> None:
    assert format_pct(0.0125) == "+1.25%"
    assert format_pct(-0.01) == "-1.00%"


def test_format_side_market_and_reason_korean() -> None:
    assert format_side_korean("SELL") == "매도"
    assert format_side_korean("SELL_REJECTED") == "매도거부"
    assert format_market_korean("domestic") == "국내"
    assert format_reason_korean("stop_loss") == "손절"
    assert format_reason_korean("order_rejected") == "주문 거부"
