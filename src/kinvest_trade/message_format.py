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
    "time_exit_cost_floor_hold": "시간 만료 후 비용기준 미달 보유",
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
    "stale_order_already_resolved": "이미 처리된 주문(취소 불필요)",
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
    "domestic_circuit_breaker_halted": "국장 연속손실 서킷브레이커 활성",
    "overseas_circuit_breaker_halted": "미장 연속손실 서킷브레이커 활성",
    "entry_market_regime_unavailable": "당일 시장환경 수집 전 진입 보류",
    "entry_market_regime_stale": "시장환경 관측치 갱신 대기",
    "post_cb_session_loss_limit_reached": "동일 시장 세션 반복손실 한도 도달",
    "domestic_order_reject_halted": "국장 매수 주문거부 차단 활성",
    "overseas_order_reject_halted": "미장 매수 주문거부 차단 활성",
    "inverse_market_closed": "시장 마감",
    "inverse_policy_unavailable": "역방향 정책 조회 실패",
    "inverse_regime_disabled": "역방향 레짐 정책 비활성",
    "inverse_execution_disabled": "역방향 실행 비활성",
    "inverse_benchmark_regime_missing": "기준지수 레짐 없음",
    "inverse_symbol_benchmark_mapping_missing": "상품별 기준지수 매핑 없음",
    "inverse_exact_benchmark_unavailable": "정확한 상품 기준지수 수집 불가",
    "inverse_benchmark_regime_stale": "기준지수 레짐 거래일 불일치",
    "inverse_benchmark_return_missing": "기준지수 등락률 없음",
    "inverse_benchmark_decline_insufficient": "기준지수 하락폭 미달",
    "inverse_benchmark_trend_unconfirmed": "기준지수 하락추세 미확인",
    "inverse_regime_shadow": "역방향 shadow 레짐 충족",
    "inverse_regime_live": "역방향 실매매 레짐 충족",
    "inverse_quote_fetch_failed": "호가 조회 실패",
    "inverse_signal_fetch_failed": "신호 조회 실패",
    "inverse_quote_or_signal_fetch_failed": "호가·신호 조회 실패",
    "inverse_benchmark_regime_unconfirmed": "기준지수 레짐 미충족",
    "inverse_product_intraday_not_up": "인버스 상품 장중 상승 미확인",
    "inverse_product_momentum_unconfirmed": "인버스 상품 양의 모멘텀 미확인",
    "inverse_product_volume_low": "인버스 상품 거래량 부족",
    "inverse_product_breakout_unconfirmed": "인버스 상품 돌파 근접 미확인",
    "inverse_severe_decline_unconfirmed": "인버스 전용 급락 기준 미충족",
    "inverse_etf_metadata_unavailable": "ETF NAV 정보 확인 불가",
    "inverse_tracking_direction_unconfirmed": "ETF 역방향 추종배수 미확인",
    "inverse_nav_unavailable": "ETF NAV 확인 불가",
    "inverse_nav_deviation_too_wide": "ETF NAV 괴리 과다",
    "inverse_entry_formula_unknown": "인버스 진입 공식 식별 불가",
    "inverse_regime_trend_breakout_entry": "급락장 인버스 추세돌파 진입",
    "inverse_dedicated_live_unvalidated": "인버스 전용 공식 실주문 미검증",
    "inverse_stop_loss": "인버스 손절",
    "inverse_hard_stop": "인버스 강제손절",
    "inverse_take_profit": "인버스 익절",
    "inverse_session_rollover": "인버스 세션종료",
    "inverse_time_exit": "인버스 시간청산",
    "inverse_benchmark_recovered": "기준지수 반등청산",
    "spread_too_wide": "스프레드 과다",
    "warmup_context": "지표 준비 부족",
    "entry_rsi_too_high": "진입 RSI 과열",
    "volume_low": "거래량 부족",
    "trend_down": "상품 단기 추세 하락",
    "setup_not_ready": "진입 조합 미충족",
    "recent_full_sell_balance_pending": "완전 체결 후 잔고 반영 대기",
    "virtual_sell_pending": "가상청산 후 실정산 대기",
}


def format_reason_korean(reason: str) -> str:
    return REASON_KOREAN_MAP.get(reason, reason)
