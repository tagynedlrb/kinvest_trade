from kinvest_trade.message_format import (
    format_domestic_symbol_label,
    format_krw,
    format_market_korean,
    format_pct,
    format_reason_korean,
    format_side_korean,
    format_usd,
)


def test_format_domestic_symbol_label_shows_name_before_code() -> None:
    assert format_domestic_symbol_label("005930", "삼성전자") == "삼성전자(005930)"


def test_format_domestic_symbol_label_falls_back_to_code_without_name() -> None:
    assert format_domestic_symbol_label("005930", "") == "005930"


def test_format_domestic_symbol_label_truncates_long_names() -> None:
    long_name = "KBSTAR 200고배당커버드콜ATM"
    label = format_domestic_symbol_label("448290", long_name)
    assert label == "KBSTAR 200고배…(448290)"
    assert len(label.split("(")[0]) <= 13


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
    assert format_side_korean("HOLD") == "보유중"
    assert format_side_korean("SELL_REJECTED") == "매도거부"
    assert format_market_korean("domestic") == "국내"
    assert format_market_korean("both") == "국내+해외"
    assert format_reason_korean("stop_loss") == "손절"
    assert format_reason_korean("order_rejected") == "주문 거부"
    assert format_reason_korean("stale_live_overseas_order_cancel") == "해외 장기미체결 취소"
    assert format_reason_korean("stale_live_overseas_order_cancel_failed") == "해외 장기미체결 취소거부"
