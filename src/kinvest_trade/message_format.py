from __future__ import annotations

DOMESTIC_STOCK_NAME_MAX_LEN = 12


def format_domestic_symbol_label(code: str, name: str) -> str:
    """Korean-name-first label for domestic stocks (e.g. "삼성전자(005930)").

    Falls back to the bare code when no name is known. Long names (some
    ETF/ETN names run well past 12 characters) are truncated so a single
    watch/trade line stays readable in Telegram.
    """
    code_text = str(code or "").strip().upper()
    name_text = str(name or "").strip()
    if not name_text:
        return code_text or "-"
    if len(name_text) > DOMESTIC_STOCK_NAME_MAX_LEN:
        name_text = name_text[:DOMESTIC_STOCK_NAME_MAX_LEN] + "…"
    return f"{name_text}({code_text})" if code_text else name_text


def format_krw(amount: float) -> str:
    rounded = int(round(amount))
    if rounded < 0:
        return f"-{abs(rounded):,}원"
    if rounded > 0:
        return f"+{rounded:,}원"
    return "0원"


def format_usd(amount: float) -> str:
    if amount < 0:
        return f"-${abs(amount):,.2f}"
    if amount > 0:
        return f"+${amount:,.2f}"
    return "$0.00"


def format_pct(ratio: float) -> str:
    sign = "+" if ratio >= 0 else ""
    return f"{sign}{ratio * 100:.2f}%"


def format_side_korean(side: str) -> str:
    mapping = {
        "BUY": "매수",
        "SELL": "매도",
        "buy": "매수",
        "sell": "매도",
        "WAIT": "대기",
        "wait": "대기",
        "HOLD": "보유중",
        "hold": "보유중",
        "BUY_SETUP": "매수준비",
        "SELL_SETUP": "매도준비",
        "SELL_REJECTED": "매도거부",
    }
    return mapping.get(side, side)


def format_market_korean(market: str) -> str:
    mapping = {
        "domestic": "국내",
        "overseas": "해외",
        "both": "국내+해외",
        "none": "없음",
    }
    return mapping.get(market, market)


REASON_KOREAN_MAP = {
    "pullback_entry": "눌림목 진입",
    "volume_breakout_entry": "거래량 돌파 진입",
    "band_breakout_entry": "밴드 돌파 진입",
    "breakout_proximity_entry": "고점 근접 진입",
    "volume_momentum_fast_entry": "급등 즉시 진입",
    "atr_hard_stop": "긴급 손절",
    "atr_soft_stop": "ATR 손절",
    "momentum_loss_cut": "모멘텀 소실 손절",
    "trend_filter_lost": "추세 이탈 손절",
    "time_exit_profit": "시간 만료 청산(수익)",
    "time_exit_loss": "시간 만료 청산(손실)",
    "time_exit_forced": "시간 만료 강제 청산",
    "partial_profit_lock": "부분 익절",
    "breakout_exhaustion_exit": "모멘텀 소진 청산",
    "marginal_profit_exit": "소수익 조기청산",
    "take_profit": "익절",
    "stop_loss": "손절",
    "watch": "감시중",
    "signal_unavailable": "신호 부족",
    "trend_holding": "추세 보유",
    "paper_test_removed_for_speed": "속도 개선으로 페이퍼 테스트 생략",
    "domestic_buy": "국내 매수",
    "strategy_buy_signal": "전략 매수 신호",
    "stale_exit_replace": "미체결 정리 후 재주문",
    "stale_live_order_cancel": "장기미체결 취소",
    "stale_live_order_cancel_failed": "장기미체결 취소거부",
    "stale_live_overseas_order_cancel": "해외 장기미체결 취소",
    "stale_live_overseas_order_cancel_failed": "해외 장기미체결 취소거부",
    "session_not_orderable_in_profile": "현재 계정에서 거래 불가한 세션",
    "order_rejected": "주문 거부",
    "trail_stop": "트레일링 스탑",
    "target_hit": "목표가 달성",
    "vwap_break": "VWAP 이탈",
    "macd_dead": "MACD 데드크로스",
    "rsi_overbought": "RSI 과열",
    "vwap_pullback": "VWAP 눌림목",
    "vol_breakout": "거래량 돌파",
    "macd_golden": "MACD 골든크로스",
    "overseas_position_cap_reached": "해외 동시보유 한도 도달(정상)",
    "total_position_cap_reached": "국내+해외 합산 한도 도달(정상)",
}


def format_reason_korean(reason: str) -> str:
    return REASON_KOREAN_MAP.get(reason, reason)
