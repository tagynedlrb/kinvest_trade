from __future__ import annotations


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
        "HOLD": "보유",
        "hold": "보유",
    }
    return mapping.get(side, side)


def format_market_korean(market: str) -> str:
    mapping = {
        "domestic": "국내",
        "overseas": "해외",
        "none": "없음",
    }
    return mapping.get(market, market)


REASON_KOREAN_MAP = {
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
    "take_profit": "익절",
    "stop_loss": "손절",
    "watch": "감시중",
    "signal_unavailable": "신호 부족",
    "trend_holding": "추세 보유",
    "paper_test_removed_for_speed": "속도 개선으로 페이퍼 테스트 생략",
    "session_not_orderable_in_profile": "현재 계정에서 거래 불가한 세션",
}


def format_reason_korean(reason: str) -> str:
    return REASON_KOREAN_MAP.get(reason, reason)
