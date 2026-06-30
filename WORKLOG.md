# WORKLOG

## 2026-06-30
### 추가 개선 15
- `TelegramNotifier`에 `set_commands()`를 추가해 텔레그램 Bot API `setMyCommands` 호출을 지원하도록 확장
- `telegram_control.py`에 `BOT_COMMANDS` 상수를 추가하고, 서비스 시작 시 `setMyCommands`를 1회 호출해 슬래시 자동완성 및 메뉴 명령 목록이 항상 등록되도록 변경
- 명령 메뉴 등록 실패는 서비스 기동을 막지 않도록 `run()`에서 예외를 삼키고 계속 START 메시지를 보내도록 처리
- `/lab_paper_test`를 메뉴에서 눌렀을 때 인자 없이 들어오는 상황을 고려해 안내 문구에 `직접 종목코드를 입력해달라`는 설명을 추가
- `tests/test_notifier.py`를 새로 추가하고, `setMyCommands` payload/반환값/disabled 동작을 검증
- `tests/test_telegram_control.py`에 `set_commands` 호출 순서, 예외 무시, 텔레그램 명령명 규칙 검증 테스트를 추가

### 추가 개선 14
- 해외/국내 매도 주문이 KIS에서 거부될 때 `submitted=False`만 반환하고 `skipped=True`가 빠져 성공처럼 `동작=매도`로 보이던 문제를 수정
- `_place_overseas_sell_order()`는 daytime/mock 세션 거부를 `session_not_orderable_in_profile`로, 그 외 거부는 `order_rejected`로 분리해 반환하도록 보강
- `_place_domestic_sell_order()`도 동일하게 `skipped=True`, `reason=order_rejected`를 명시하도록 수정
- `_format_order_summary()`에 `SELL_REJECTED` 상태를 추가하고, 텔레그램 표시값은 `매도거부`로 매핑
- `_send_summary()`가 `SELL_REJECTED`일 때는 알림을 숨기지 않고, `참고=주문이 거부되어 실제로 체결되지 않았습니다` 문구를 추가해 실제 미체결 상태를 명확히 알리도록 조정
- `_place_domestic_test_order()`에 `KisApiError` 방어 처리를 추가해 장중 거부·일시 정지 등 예외 상황에서도 런타임 예외 전파 없이 `skipped=True`로 정리되도록 보강
- `/lab_paper_test` 명령은 이미 코드에 반영된 상태임을 재확인했고, 재적용은 하지 않음
- 추가 점검 결과:
  - `_select_overseas_exit_target()`은 `고정 손절/익절 우선 -> signal cache 기반 청산` 2단계 구조
  - `_select_domestic_exit_target()`은 `watch_targets`에서 이미 `_build_exit_setup()`을 거친 `SELL_READY` 후보만 고르는 구조
  - 완전한 내부 구현 대칭은 아니지만, 둘 다 최종적으로 `_build_exit_setup()` 계열 판단을 기반으로 청산 대상을 고르는 점은 일관됨

### 추가 개선 13
- 첨부된 `11차 개선`, `12차 개선` 지시문을 기준으로 `liquidity_lab.py`에 `DomesticHeldPosition`, `_load_domestic_positions()`, `_select_domestic_exit_target()`, `_place_domestic_sell_order()`를 추가해 국내 보유 포지션 추적과 실제 국내 매도 경로를 연결
- 국내 `watch_targets`가 이제 보유 포지션을 인식하고, `domestic_top_n` 밖의 보유 종목도 강제로 감시에 포함하도록 수정
- `LiquidityLabReport`와 텔레그램 컨트롤러 요약에 `domestic_positions`를 추가하고 `/lab_positions`가 국내·해외 보유분을 함께 보여주도록 확장
- `scan_domestic()`를 `quote-only 1차 스캔 + 상위 후보 chart 정밀 스캔` 2단계 구조로 바꾸고, 남아 있던 `asyncio.sleep(0.1/0.2)`를 `0.05` 기준으로 정리
- 자동 사이클에서 국내 `paper-run` 25초 검증을 제거하고, 수동 검증용 텔레그램 명령 `/lab_paper_test <종목코드>`를 추가
- 텔레그램 명령 `/lab_service_restart`를 추가해 `kinvest-telegram-control.service`를 봇에서 직접 재시작할 수 있게 연결
- 재시작 후 `telegram getUpdates offset`가 초기화되며 같은 `/lab_service_restart` 명령을 다시 읽어 무한 재시작 루프에 들어가는 문제를 수정
- `state/runtime_state.json`에 `telegram_update_offset`를 저장하고, 서비스 시작 시 복구하도록 변경
- `message_format.py`와 `format_kst_korean()`을 도입해 `liquidity_lab`, `auto_trader`, `telegram_control` 알림을 한국어/KST 중심의 짧은 형식으로 단순화
- 해외 mock 계정이 거래 불가한 daytime/premarket 세션에서도 `overseas_buy_target`이 있으면 매수 시도를 하던 버그를 수정해, 이제 `us_orderable_in_profile=False`일 때는 `session_not_orderable_in_profile`로 건너뛰도록 변경
- 반대로 보유 해외 포지션 청산 후보는 `us_orderable_in_profile`와 무관하게 계산하고 매도 시도 경로를 유지하도록 보강
- `liquidity_lab` 요약 메시지의 WAIT 비교를 한글 `대기`가 아닌 `action_raw` 원본 값 기준으로 바꿔, 대기 상태 알림이 매 사이클 반복 발송되던 문제를 수정
- `tests/test_message_format.py`를 새로 추가하고, 국내 매도/국내 잔고/새 텔레그램 메시지 포맷에 맞춰 관련 테스트를 확장

### 검증 결과
- `pytest -q tests/test_message_format.py tests/test_time_utils.py tests/test_liquidity_lab.py tests/test_telegram_control.py tests/test_auto_trader.py` 통과 (`43 passed`)
- `python3 -m compileall src` 통과

## 2026-06-29
### 추가 개선 12
- 첨부된 `10차 개선` 지시문 기준으로 `auto_trader.py`의 SELL 알림 포맷을 보강해 `avg_price=0` 복구 실패 시 `buy_price=unknown`, `pnl_pct=unknown`으로 명확히 표기하도록 수정
- 같은 SELL 알림에 `gross_usd`를 추가해 수수료 차감 전 손익과 순손익을 함께 비교할 수 있게 조정
- `_sync_startup_position()`에서 브로커 평균매입가가 0으로 들어오면 `POSITION_AVG_PRICE_FALLBACK` heartbeat를 기록하도록 방어 로직 추가
- `liquidity_lab.py`의 `_place_overseas_sell_order()` 성공 경로에 `[KIS][LAB_SELL]` 텔레그램 알림을 추가해 lab 직접 매도도 즉시 추적 가능하게 변경
- `tests/test_auto_trader.py`에 unknown avg_price, gross_usd, 정상 SELL 필드, avg_price fallback heartbeat 케이스를 추가
- `tests/test_liquidity_lab.py`에 LAB_SELL 성공/실패/avg_price 미상 케이스를 추가

### 검증 결과
- `python3 -m pytest tests/ -q` 통과 (`99 passed`)
- `python3 -m pytest tests/test_auto_trader.py -q` 통과 (`12 passed`)
- `python3 -m pytest tests/test_liquidity_lab.py -q` 통과 (`9 passed`)
- `python3 -m compileall src` 통과

## 2026-06-29
### 추가 개선 11
- 첨부된 `9차 개선` 지시문 기준으로 `market_sessions.py`에 `minutes_until_next_tradeable_session()`와 `determine_loop_interval_sec()`를 추가해 장 상태 기반 동적 루프 간격 계산을 도입
- `telegram_control.py`에서 `no_supported_market_open` 시 auto-stop을 제거하고, 장이 닫혀도 `running` 상태를 유지한 채 다음 장까지 대기하도록 변경
- `TelegramLiquidityLabController`에 `_consecutive_errors`, `_last_market_state`를 추가하고, 연속 오류 누적/복구 및 장 상태 변화 텔레그램 알림을 반영
- `/lab_status` 메시지에 현재 장 상태, 다음 루프 간격, 연속 오류 횟수를 추가
- `liquidity_lab.py`는 양쪽 장이 모두 닫힌 경우 API 호출 없이 즉시 `market_closed` 리포트를 반환하도록 조정
- `tests/test_market_sessions.py`에 다음 세션까지 남은 시간과 동적 간격 계산 테스트를 추가
- `tests/test_telegram_control.py`에 market-closed 시 running 유지, 오류 누적, 성공 시 오류 카운터 초기화 테스트를 추가

### 검증 결과
- `python3 -m pytest tests/ -q` 통과 (`93 passed`)
- `python3 -m pytest tests/test_market_sessions.py -q` 통과 (`17 passed`)
- `python3 -m pytest tests/test_telegram_control.py -q` 통과 (`10 passed`)
- `python3 -m compileall src` 통과

## 2026-06-29
### 추가 개선 10
- 첨부된 `8차 개선` 지시문 기준으로 `liquidity_lab` 해외 루프를 `69개 전체 quote + 상위 15개/보유 종목 signal 캐시` 구조로 재조정
- `scan_overseas()` 반환을 `(ranked_results, held_symbols)`로 바꾸고, quote sleep과 signal sleep을 각각 `0.05초`로 축소
- `__init__`에 `_signal_cache`를 추가하고, 같은 사이클의 `_build_overseas_watch_targets`와 보유 종목 청산 판단이 이 캐시를 재사용하도록 변경
- `_build_overseas_watch_targets()`에서 chart API 재호출과 `asyncio.sleep(0.1)`을 제거해 감시 목록 빌드 비용을 줄임
- `_place_overseas_test_order()`도 캐시 우선 조회 후 누락 시에만 fallback 로드하도록 보강
- `config/fixed_config.json`의 `overseas_scan_top_n=15`, `loop_interval_sec=20`, `intraday_chart_refresh_sec=20`으로 운영값을 조정
- `_estimate_api_calls_per_cycle()`를 새 구조 기준으로 갱신해 기본 미국장 추정 호출량이 `101 / cycle` 수준이 되도록 정리
- `tests/test_overseas_scan.py`를 반환 타입 변경과 signal cache 검증 케이스 중심으로 확장

### 검증 결과
- `python3 -m pytest tests/ -q` 통과 (`83 passed`)
- `python3 -m pytest tests/test_overseas_scan.py -q` 통과 (`9 passed`)
- `python3 -m compileall src` 통과

## 2026-06-29
### 추가 개선 9
- 첨부된 `7차 개선` 지시문 기준으로 `liquidity_lab` 해외 감시 구조에서 `active_pool / bench_scan / pool_rotation` 2단계 구조를 완전히 제거
- `scan_overseas()`를 `69개 전체 quote 스캔 -> activity_score 정렬 -> held 포함 signal 우선순위 부여` 단일 패스로 재작성
- 설정 스키마에서 `overseas_top_n`, `overseas_active_pool_size`, `overseas_bench_scan_every`, `overseas_min_active_pool_size`를 제거하고 `overseas_scan_top_n`을 추가
- `config/fixed_config.json`의 해외 후보군을 대형 고유동성 69종목으로 교체하고, `overseas_scan_top_n=69`로 기본 설정
- `_estimate_api_calls_per_cycle()`를 새 구조 기준으로 갱신해 미국장 기본 추정 호출량이 `211 / 15s`로 계산되도록 정리
- `tests/test_pool_rotation.py`를 `tests/test_overseas_scan.py`로 전환하고, 전체 스캔/held 우선 포함/제외 후보/API 호출량 검증 케이스로 재작성
- `README.md`의 해외 감시 설명을 새 단일 스캔 구조 기준으로 갱신

### 검증 결과
- `python3 -m pytest tests/ -v` 전체 통과 목표로 반영
- `POOL_ROTATION` heartbeat와 관련 상태 변수가 코드에서 완전히 사라졌는지 함께 점검 예정

## 2026-06-29
### 추가 개선 8
- 첨부된 `6차 개선` 지시문 기준으로 `liquidity_lab` 해외 후보군을 20개에서 50개로 확장하고, `active_pool=8`, `watchlist=8` 기준으로 재조정
- `overseas_min_active_pool_size=0`을 도입해 조건 미달 종목으로 active pool을 억지로 채우지 않도록 변경
- `scan_overseas()`가 cycle 시작 시 `_get_held_symbols()`를 먼저 갱신하고, `_run_bench_scan()`에서는 `_last_held_symbols`를 `held_pinned`로 active pool에 우선 포함하도록 보강
- `POOL_ROTATION` heartbeat에 `held_pinned=[...]` 필드를 추가해 보유 종목 pinning 여부를 바로 추적할 수 있게 함
- `_estimate_api_calls_per_cycle()`를 50개 bench scan + held 추가 감시 구조에 맞춰 갱신
- `auto_trader.py`의 SELL 텔레그램 알림에 `buy_price`, `pnl_usd`, `pnl_pct`, `pnl_krw`, `cum_pnl`, `hold`를 추가
- SELL 알림 정확도를 위해 `_apply_sell_fill` 전 `avg_price_before_fill`, `hold_cycles_before_fill`을 캡처해 `_send_fill_message()`에 전달
- `tests/test_pool_rotation.py`에 empty pool 허용, held pinning, filtered exclusion, heartbeat 검증 케이스를 추가
- `tests/test_auto_trader.py`에 SELL 메시지의 USD 손익/수익률/보유시간 검증 케이스를 추가

### 검증 결과
- `python -m pytest tests/ -v` 통과 목표로 반영
- 영향 범위 부분 테스트 통과 후 전체 스위트로 확장 예정

## 2026-06-29
### 추가 개선 7
- 첨부된 `5차 개선` 지시문 기준으로 `RSI 68 상단 차단`을 완화하고, `max_entry_rsi14=85.0`까지 급등 구간 진입을 허용
- `volume_spike_ratio=1.1`, `breakout_proximity_pct=0.98`를 적용해 직전 고점 98% 근접 구간의 선제 진입을 허용
- `trend_require_price_above_slow=false` 기본값을 추가해 `price < slow_ma`라도 `fast_ma >= slow_ma`면 반등 초입 진입을 볼 수 있게 조정
- `momentum_policy.py`의 `evaluate_entry_setup`을 개편해 `fast_track` 기준을 `volume_spike_ratio × 1.6`으로 완화하고, `near_breakout` 대기 상태와 `breakout_proximity_entry` 진입 경로를 추가
- `technical_signals.py`의 `has_required_context`를 `daily_ma_fast + minute_ma_fast`만으로도 True가 되도록 완화해 장 시작 직후 10분 WARMUP 구간을 줄임
- `derive_watch_state`와 `liquidity_lab`의 watch target 상태를 새 state 체계(`BUY/READY/WAIT/SKIP/WARMUP`)에 맞게 반영
- `tests/test_momentum_policy.py`에 RSI, proximity, adaptive trend filter, fast-track, reduced warmup 관련 케이스를 추가

### 검증 결과
- `python -m pytest tests/ -v` 통과 (`79 passed`)
- 샘플 스냅샷 검증:
  - 기본 설정에서 `trend_down`이면 `WAIT`
  - `trend_require_price_above_slow=false`에서는 같은 흐름이 `breakout_proximity_entry`로 `BUY` 전환됨

## 2026-06-26
### 추가 개선 6
- 첨부된 `4차 개선` 지시문 기준으로 `momentum_policy.py`의 `time_exit`를 손실 포지션에도 적용되도록 재설계
- 최대 보유 시간 도달 시 수익 포지션은 `time_exit_profit`, 손실 포지션은 추세 이탈 시 `time_exit_loss`, 장기 방치 시 `time_exit_forced`로 정리하도록 보강
- `auto_trade`에 `use_slot_sizing`, `slot_entry_pct`, `slot_scale_in_pct`, `slot_max_pct`를 추가하고, 수량을 고정 주식 수가 아닌 가용 달러 기준으로 역산하는 슬롯 기반 금액 운용을 도입
- `auto_trader.py`에 `last_available_usd` 캐시를 추가하고, 주문가능조회 응답에서 가용 외화 금액을 읽어 슬롯 기반 수량 계산에 연결
- 가용 금액을 읽지 못하는 경우에는 `last_available_usd=0`으로 두고 기존 고정 수량 로직으로 자동 폴백하도록 유지
- `telegram_control.py`에 `/lab_positions` 명령을 추가해 현재 보유 포지션과 미실현 손익을 조회할 수 있게 함
- `watchlist` 출력에도 보유 종목의 `pnl=+X.XX%`를 함께 표기하도록 확장
- `tests/test_momentum_policy.py`를 새로 추가하고, `tests/test_auto_trader.py`, `tests/test_telegram_control.py`를 새 슬롯/포지션 조회 동작에 맞춰 확장

### 검증 결과
- `python -m pytest tests/ -v` 전체 통과 목표로 반영
- 슬롯 기반 수량 계산과 손실 time-exit, 텔레그램 positions 출력 테스트 추가

## 2026-06-26
### 추가 개선 5
- 첨부된 `3차 개선` 지시문 기준으로 `auto_trade`의 손익/진입 문턱을 현실화
- `take_profit_pct=0.006`, `full_take_profit_pct=0.012`, `stop_loss_pct=0.004`, `hard_stop_loss_pct=0.008`로 조정
- `min_expected_reward_cost_ratio`를 `0.5`로 낮춰 왕복 비용 대비 기대수익 필터가 수학적으로 항상 실패하던 문제를 해소
- `volume_spike_ratio=1.2`, `breakout_lookback_bars=3`, `scale_in_volume_ratio=1.0`, `min_intraday_momentum_pct=0.0008`, `min_bar_return_pct=0.0003`, `max_breakout_extension_pct=0.008`으로 완화
- `auto_trader.py`의 `_entry_has_sufficient_edge`에 `EDGE_FAIL_COST`, `EDGE_FAIL_RISK` heartbeat를 추가해 진입 차단 원인을 로그로 남기도록 보강
- `liquidity_lab.py`에 `_last_held_symbols` 캐시와 `_get_held_symbols()`를 추가하고, active pool에서 밀린 보유 종목도 다음 사이클 스캔 대상에 강제로 포함하도록 수정
- API 호출량 추정에 `held_check + position_load` 2회 balance 조회를 반영
- 해외 후보군에서 `MARA`, `RIVN`을 제거하고 `COIN`, `NFLX`로 교체
- `tests/test_auto_trader.py`, `tests/test_pool_rotation.py`를 새 edge 로그와 held-symbol 보호 경로에 맞춰 확장

### 검증 결과
- `python -m pytest tests/ -v` 전체 통과 목표로 반영
- held 종목 캐시 fallback과 active pool 밖 보유 종목 강제 스캔 테스트 추가

## 2026-06-26
### 추가 개선 4
- 첨부된 `2차 개선` 지시문 기준으로 `adaptive_params.py`의 `weak_flow` 분기를 제거해 저유동성 구간에서 `volume_spike_ratio` 문턱을 억지로 높이지 않도록 수정
- `momentum_policy.py`의 `evaluate_entry_setup` 검사 순서를 `spread -> context -> RSI -> volume -> fast-track -> trend -> momentum -> extension -> breakout`으로 재정렬
- 이에 따라 거래량 확장이 부족한 경우 `trend_filter_off`보다 먼저 `volume_not_expanded` 사유가 남고, `fast-track` 진입은 추세 필터를 우회할 수 있게 정리
- `liquidity_lab` 해외 감시를 고정 풀 스캔에서 `벤치 풀 20개 + active pool 5개 + 4사이클마다 재선정` 구조로 전환
- `POOL_ROTATION` heartbeat에 `cycle`, `bench_scanned`, `passed_filter`, `active_pool` 정보를 남겨 운영 중 풀 교체를 추적할 수 있게 함
- `tests/test_pool_rotation.py`를 추가하고, 저장소 기본 스타일에 맞춰 `pytest-asyncio` 없이 `asyncio.run(...)` 기반 테스트로 구성
- `README.md`에 active pool 로테이션과 새 해외 후보군 설명을 반영

### 검증 결과
- `PYTHONPATH=src pytest -q tests/test_adaptive_params.py tests/test_pool_rotation.py tests/test_liquidity_lab.py` 통과 목표로 갱신
- 풀 로테이션 테스트가 더 이상 플러그인 부재로 skip되지 않도록 정리

## 2026-06-26
### 추가 개선 3
- 첨부 지시문 기준으로 `5분봉 기반 breakout`을 `1분봉 단타` 구조로 전환
- `config/fixed_config.json`의 `auto_trade`를 1분봉, 10초 폴링, 5분 최대 보유 기준으로 조정
- `liquidity_lab` 스크리닝 주기를 `15초`로 줄이고, 해외 최소 거래량/국내 최소 거래대금 문턱을 완화
- `adaptive_params.py`를 새로 추가해 ATR, 거래량 강도, 모멘텀 기반 동적 손익/진입 기준 override를 구현
- `auto_trader.py`에 adaptive override를 연결하고 `ADAPTIVE_OVERRIDE` heartbeat 저장 경로를 추가
- `momentum_policy.py`에 `volume_momentum_fast_entry` fast-track 진입 경로를 추가
- `liquidity_lab.py`의 국내/해외 `activity_score`에 거래대금 급증/거래량 급증/타이트 스프레드 보너스를 반영
- `technical_signals.py`의 `has_required_context`를 완화해 1분봉 warmup 대기 시간을 줄임
- `tests/test_adaptive_params.py`를 새로 추가하고 관련 전략 테스트를 1분봉 기준으로 갱신

### 검증 결과
- `python3 -m compileall src` 통과
- `PYTHONPATH=src pytest -q tests` 통과 예정 확인용 부분 테스트 선행

## 2026-06-26
### 추가 개선 2
- 사용자 설계 검토를 반영해 `이평선 단독 트리거`를 폐기하고 `거래량 폭발 + 가격 돌파`를 메인 진입 신호로 재편
- `technical_signals.py` 스냅샷에 `volume_ratio`, `breakout_level`, `ATR`, `Bollinger`, `intraday_bar_return`를 추가
- KIS 실응답 확인 결과:
  - 해외 5분봉 분봉 거래량 필드는 `evol`
  - 국내 분봉 거래량 필드는 `cntg_vol`
  - 해외 분봉 고가/저가 필드는 `high`, `low`
- 새 공통 정책 모듈 `momentum_policy.py`를 추가해 `entry / scale-in / exit` 판단을 `auto_trader`, `liquidity_lab`가 함께 쓰도록 정리
- `python3 main.py` 기본 자동매매를 `OVERSEAS_LIQUIDITY_MOMENTUM` 정책으로 변경
- `liquidity-lab` 감시/주문 판단도 같은 정책으로 맞추고, `signal_score`가 가장 강한 감시 대상을 우선 주문 대상으로 선택하도록 보강
- 텔레그램 감시 목록은 기존 이평 관계와 함께 `vr=...x`, `mom=...%` 식의 짧은 상태 메모를 표시하도록 변경

### 검증 결과
- `python3 -m compileall src` 통과
- `PYTHONPATH=src pytest -q tests/test_auto_trader.py tests/test_liquidity_lab.py tests/test_telegram_control.py` 통과 (`13 passed`)

## 2026-06-26
### 사용자 지시
- 현재 주먹구구식 단기매매 대신 이동평균 기반 방식을 1순위 전략으로 적용
- `python3 main.py` 기본 경로를 계속 감시/거래하는 방식으로 유지하되, `20회 체결 후 종료`는 제거
- 텔레그램과 테스트 출력은 더 짧고 알아보기 쉬운 형태로 정리
- `start`부터 `pause/stop`까지의 세션 성과를 종목별 요약과 손익 중심으로 남기기
- 원격 저장소에 푸시 가능한 형태로 `.gitignore`를 먼저 정리하고 git 작업 흐름을 연결

### 이번에 수행한 내용
- 원격 저장소 `tagynedlrb/kinvest_trade.git`를 점검한 결과, 원격에는 `README.md`만 있는 초기 커밋 상태임을 확인
- `.gitignore`를 확장해 `.env`, `keys/`, `data/`, `logs/`, `state/`, 가상환경, 캐시 파일, 임시 디렉터리 등을 기본 제외 대상으로 정리
- KIS 해외 차트 조회를 위해 공식 엔드포인트 기준 `dailyprice(HHDFS76240000)`와 `inquire-time-itemchartprice(HHDFS76950200)` 래퍼를 `client.py`에 추가
- 공통 이동평균 신호 계산 모듈 `technical_signals.py`를 추가
- 기본 자동매매를 `OVERSEAS_MA_BAND` 전략으로 교체
- 일봉 `20/60 이동평균`, 5분봉 `5/20 이동평균`, RSI, 단기 변동성을 함께 써서 `60일선 근처 진입`, `20일선 추세 복귀 진입`, `20일선 이탈 손절`, `60일선 실패 강제청산`, `20일선 재접근 절반익절` 구조로 재설계
- `max_actions_per_run=0`, `max_decision_cycles_per_run=0`이면 제한 없이 계속 감시하도록 해석하게 바꾸고, 실제 기본 설정도 무제한으로 변경
- 기본 자동매매는 이제 `수동 중지 전까지` 또는 `현재 프로필에서 주문 가능한 장이 끝날 때까지` 계속 실행되도록 변경
- `liquidity-lab` 해외 주문도 `현재 가장 활발한 종목`을 고른 뒤, 이동평균 진입 조건이 맞을 때만 주문을 넣도록 보강
- 해외 보유 포지션 청산 우선순위에 `20일선/60일선 이탈` 기반 이동평균 청산 신호를 추가
- 텔레그램 컨트롤러 상태/세션 요약을 `confirmed_pnl_krw`, `estimated_exit_pnl_krw`, `symbols=...` 중심의 짧은 형태로 재정리
- 세션별 종목 요약에 `buy/sell 횟수`, `paper run 횟수`, `국내 확정 손익`, `해외 청산 추정 손익`을 누적하도록 확장
- README를 새 전략과 무제한 실행 정책 기준으로 갱신
- 클라이언트/전략/텔레그램/유동성랩 관련 테스트를 새 동작에 맞게 갱신

### 검증 결과
- `pytest -q tests` 통과 (`47 passed`)
- `python3 -m compileall src` 통과
- `python3 main.py doctor` 실행 확인
- 실제 KIS 모의 계정으로 `SOXL` 현재가 + 일봉 + 5분봉 조회 확인
- 새 이동평균 스냅샷 계산 확인:
  - `last_price=218.03`
  - `daily_bars=100`
  - `minute_bars=60`
  - `regime=trend_down`
  - `indicator=rsi=46.6, 20d=-8.00%, 60d=+31.68%`

## 2026-06-26
### 추가 개선
- `liquidity-lab`가 더 이상 1개 종목만 기계적으로 기다리지 않도록, 현재 열린 시장의 `top N` 후보를 동시에 감시하고 그중 이동평균 신호가 뜬 종목만 주문 대상으로 선택하도록 보강
- 텔레그램 `WAIT` 로그는 중단하고, 실제 `BUY/SELL 제출` 또는 `주문 오류` 때만 알림을 보내도록 축소
- 텔레그램 명령 `/lab_watchlist` 추가
- `/lab_watchlist`는 현재 감시중인 종목 목록을 `종목코드 / 상태 / 이평 관계 / 짧은 사유 / 가격` 한 줄 형식으로 보여주도록 구현
- 컨트롤러 상태 파일과 최근 리포트에 `watch_targets`, `estimated_api_calls_per_cycle` 저장

### 확인 메모
- 공개 확인 가능한 한국투자증권 공식 자료 기준:
  - 포털 공지에 `API 호출 유량 안내 (REST, 웹소켓) (2026.04.20 기준)` 존재
  - 오류코드 `EGW00201`은 `초당 거래건수 초과`로 안내됨
  - 다만 공개 fetch 가능한 페이지에서는 유량 숫자 표가 노출되지 않아, 현재 구현은 보수적으로 headroom을 남기는 방향으로 설계
- 실제 현재 설정(`overseas_candidates=6`, `overseas_top_n=3`, `loop_interval_sec=30`) 기준 dry-run 리포트 확인:
  - `watch_count=3`
  - `estimated_api_calls_per_cycle=14`
  - 평균 호출량은 약 `0.47회/초`
  - 순차 호출과 짧은 sleep을 넣어 순간 burst도 낮게 유지

## 2026-06-25
### 사용자 지시
- SOXL 자동매매 전략을 더 현실적으로 고도화
- 손절 기준을 단일 퍼센트가 아니라 변동성과 민감도에 따라 나누기
- 매수 다음이 무조건 매도가 아니도록 하고 수량도 가변적으로 판단하게 만들기
- 환차익, 수수료, 세금 추정치를 포함한 손익 계산으로 확장
- 거래량이 높은 국내/해외 종목 후보를 같이 비교해서 타겟을 잡고, 전략 테스트와 모의주문 결과를 반영해 업그레이드

### 이번에 수행한 내용
- `SOXL_MICRO_SCALP`를 `SOXL_VOLATILITY_AWARE` 전략으로 교체
- 최근 가격 이력 기반 `momentum`, `volatility`, `drawdown` 계산 함수 추가
- 자동매매 로직을 `초기 시드 진입 + 추세/눌림 재진입 + 분할매수 + 분할매도 + 소프트/하드 손절 + 트레일링 익절` 구조로 확장
- `max_position_qty`, `allow_scale_in`, `allow_partial_exit`, `scale_in_cooldown_cycles` 등 가변 포지션 파라미터 추가
- 해외주식 주문가능조회 응답의 `exrt`를 사용해 체결 시점 환율 추정치를 손익 계산에 반영
- `commission_rate`, `sec_fee_rate`, `fx_fee_rate`, `annual_tax_free_allowance_krw`, `capital_gains_tax_rate` 설정 추가
- 자동매매 실행 이력 테이블에 `realized_pnl_net_usd`, `realized_pnl_net_krw`, `fees_usd`, `fx_pnl_krw`, `estimated_tax_delta_krw` 저장 칼럼 추가
- 최종 런 요약에도 순손익, 누적 비용, 환차손익, 세금 추정치를 남기도록 확장
- README를 새 전략과 손익 계산 기준에 맞게 갱신
- 새 지표/비용 계산용 테스트 추가
- `liquidity_lab` 설정 섹션과 `liquidity-lab` CLI 추가
- 국내/해외 후보군을 각각 현재 거래량, 거래대금, 스프레드, 단기 모멘텀으로 점수화하는 `LiquidityLabService` 추가
- 국내 장중에는 상위 3개 후보로 짧은 paper test를 수행하고, `DRY_RUN=false`이면 상위 국내 후보에 mock 주문까지 넣도록 확장
- KIS가 응답 헤더 없이 연결을 끊는 경우를 대비해 transport 재시도 로직을 추가
- 시장 시간 판별용 `market_sessions.py` 추가
- 자동매매에서 `관망/패스`를 명시적인 전략 턴으로 취급하도록 변경
- `max_actions_per_run=20`은 유지하되, `max_decision_cycles_per_run` 안에서 여러 번 skip한 뒤 20회 체결에 도달할 수 있게 실행 구조를 수정
- 최종 요약에 `decision_count`, `skip_count`, `completion_reason` 추가
- `liquidity-lab`의 타겟 선정 기준을 단순 거래량 순위에서 `activity_score` 기반으로 변경
- 현재 장이 열린 시장에서 가장 활발하게 거래되는 1개 종목만 `primary_target`으로 선택해 테스트하도록 조정
- `telegram-control` 데몬 추가
- 텔레그램 봇 명령 `/lab_start`, `/lab_pause`, `/lab_resume`, `/lab_stop`, `/lab_terminate`, `/lab_status`로 `liquidity-lab` 반복 실행 루프를 원격 제어할 수 있게 확장
- 컨트롤러 상태를 `state/runtime_state.json`에 기록하도록 확장
- `systemd --user` 서비스 유닛 `kinvest-telegram-control.service` 추가
- 텔레그램 컨트롤러가 백그라운드에서 계속 살아 있으면서, `liquidity-lab` 종료 후에도 다음 텔레그램 명령을 기다리도록 semantics 조정

### 검증 결과
- `pytest -q tests` 통과 (`16 passed`)
- `python3 -m compileall src` 통과
- `python3 main.py doctor` 실행 확인
- `python3 main.py overseas-price-check SOXL --exchange AMEX` 실행 확인
- `python3 main.py` 기본 경로 실행 시, `2026-06-25 UTC` 기준 KIS 모의투자 응답 `40580000 모의투자 장종료 입니다.` 확인
- 전략 코드 자체는 실행 경로에 연결되었고, 장중에는 같은 명령으로 바로 모의자동매매를 재시험할 수 있는 상태
- `python3 main.py liquidity-lab` 실행 결과:
  - 국내 상위 후보: `000660`, `005930`, `010170`
  - 해외 상위 후보: `AAL`, `NVDA`, `INTC`
  - 국내 paper test run_id=2 결과 `realized_pnl_krw=0`
  - 국내 mock 매수주문 성공: `000660` 1주, `ODNO=0000029604`
  - 국내 mock 매도주문 성공: `000660` 1주, `ODNO=0000029629`
  - 왕복 후 모의계좌 총평가금액 `9,994,360원`으로 반영되어, 작은 호가 차익만으로는 실제 비용을 넘기 어렵다는 점 확인

## 2026-06-24
### 사용자 지시
- 실계좌와 모의투자계좌의 appkey / appsecret / 계좌정보를 어디에 넣어야 하는지 명확히 정리
- 모의투자 계좌로 전환하여 매수/매도 테스트를 수행할 수 있게 준비
- README, WORKLOG 등 문서를 현재 단계에 맞게 갱신

### 이번에 수행한 내용
- 설정 로더를 확장해 `실계좌(prod)`와 `모의투자(vps)` 키/계좌 세트를 동시에 보관할 수 있게 변경
- `KIS_ENV` 값으로 활성 프로필을 전환하도록 정리하고 `prod/live`, `vps/paper/mock` 별칭 지원 추가
- 활성 프로필 기준으로 `keys/prod_appkey.txt`, `keys/prod_appsecret.txt`, `keys/vps_appkey.txt`, `keys/vps_appsecret.txt`를 읽도록 구성
- `balance-check`, `orderable-check`, `order-test` CLI 추가
- 8자리 계좌번호만 입력된 경우 상품코드 `01`을 자동 보정하고, `12345678-01` 형식도 허용하도록 보완
- 프로세스 간 토큰 캐시를 추가해 KIS `접근토큰 발급 1분당 1회` 제한에 덜 걸리도록 개선
- `SOXL` 테스트를 위해 해외주식 전용 `overseas-price-check`, `overseas-balance-check`, `overseas-orderable-check`, `overseas-order-test` CLI 추가
- 해외주식 현재가 조회 시 `AMEX -> AMS`, `NYSE -> NYS`, `NASD -> NAS` 조회코드 변환 로직 추가
- `python3 main.py`만으로 동작하는 `SOXL_MICRO_SCALP` 자동매매 루프 추가
- `auto_trade` 설정 섹션을 도입해 대상 심볼, 거래소, 폴링 간격, 액션 수, 진입/청산 규칙을 고정 설정으로 관리하도록 변경
- 자동매매 실행 이력을 `auto_trade_runs`, `auto_trade_actions` 테이블에 저장하도록 추가
- 모의투자 미국주식 매도 TR을 `VTTT1001U`로 보정
- 모의투자 매도 시 KIS 잔고 반영 지연이 있으면 다음 사이클에 재시도하도록 자동 루프 보완
- `order-test`는 기본 preview 모드로 두고, `--execute`가 있을 때만 주문 호출하도록 안전장치 추가
- 실계좌 주문은 `DRY_RUN=false`, `LIVE_TRADING_ENABLED=true`, `--confirm-live EXECUTE_LIVE`를 모두 만족할 때만 허용
- `.env.example`, `README.md`를 실사용 기준으로 전면 갱신
- 설정 로더용 테스트를 추가해 실계좌/모의투자 변수 분리를 검증

### 검증 결과
- `pytest -q tests` 통과
- `python3 main.py doctor` 실행 확인
- `python3 main.py order-test buy 005930 --qty 1 --price 70000 --order-division 00` preview 출력 확인
- 모의투자 `auth-check` 성공, 실계좌 `auth-check` 성공
- 실계좌/모의투자 해외주식 `SOXL` 현재가 조회 성공
- 실계좌/모의투자 해외주식 잔고 조회 성공
- 모의투자 `SOXL` 매수가능수량 조회 성공: `max_order_quantity=447` 확인
- 모의투자 `SOXL` 1주 매수주문 제출 성공: `ODNO=0000059102`
- 모의투자 `SOXL` 매도주문은 `VTTT1001U` 보정 후 정상 제출 성공 확인
- `python3 main.py` 자동실행 run_id=3 완료: 총 20회 액션, `BUY 10`, `SELL 10`, `realized_pnl_usd=-0.4020`
- 자동 실행 중 체결 알림과 최종 요약을 텔레그램으로 전송하는 경로까지 함께 사용

## 2026-06-23
### 사용자 지시
- `kiwoom_trade`를 참고해 `~/kinvest_trade`에서 한국투자증권 리눅스 API 방식으로 다시 개편
- 기존 프로젝트와 `디자인 목표`, `동작 방식`, `구조`는 동일하게 유지
- KIS에서 쓸 수 있는 API는 적극 활용
- 키는 아직 없으므로 `테스트 직전 단계`까지 먼저 준비
- 주석은 사용자가 읽고 이해하기 쉽게 더 꼼꼼히 작성
- 앞으로도 지시사항과 수행 내용을 파일에 계속 기록

### 이번에 수행한 내용
- `kinvest_trade` 새 프로젝트 골격 생성
- `config/fixed_config.json`, `state/runtime_state.json`, `run_watch.py`, `run_telegram_test.py` 구조 유지
- KIS REST 기준으로 `tokenP`, `inquire-price`, `inquire-asking-price-exp-ccn`, `inquire-daily-itemchartprice`, `inquire-time-dailychartprice` 래퍼 작성
- 실주문 대신 실데이터 기반 `paper trading` 루프 작성
- 텔레그램은 `kinvest_trade/keys`가 비어 있어도 `~/kiwoom_trade/keys`를 fallback으로 읽도록 구성
- `doctor`, `auth-check`, `indicator-check`, `paper-run`, `paper-report`, `telegram-test` 명령 정리
- README와 PLAN 문서 작성

### 현재 상태
- 텔레그램 테스트는 현재 구조상 바로 가능
- KIS 키가 없으므로 `auth-check`, `indicator-check`, `run_watch.py`의 실API 검증은 다음 단계
- 기본 감시 종목은 `005930`

### 다음 검증 예정
- `python3 main.py auth-check`
- `python3 main.py indicator-check 005930 --timeframe minute`
- `KIS_WATCH_MAX_CYCLES=1 python3 run_watch.py`
- `python3 main.py paper-run --iterations 5 --interval-sec 15`

### 검증 결과
- `python3 -m pytest` 통과
- `python3 main.py doctor` 실행 확인
- `python3 run_telegram_test.py` 실행 결과 `telegram_sent=true` 확인
- `python3 main.py auth-check` 실행 시, 현재 KIS 키 미설정 상태를 명확한 JSON 메시지로 안내하는 것 확인

## 2026-06-25
### 이번에 수행한 내용
- KIS 미국장 세션 판단을 `주간거래 / 프리마켓 / 정규장 / 애프터마켓`으로 세분화
- 한국투자증권 공식 샘플 저장소 기준으로 미국주간주문 전용 엔드포인트(`/uapi/overseas-stock/v1/trading/daytime-order`)와 TR ID(`TTTS6036U`, `TTTS6037U`)를 확인
- 실계좌는 미국주간 세션에서 전용 주문 API를 사용하도록 경로를 추가
- 모의계좌는 공식 응답대로 `미국주식 주간거래 미지원` 상태를 명확히 감지하고, 더 이상 `no_supported_market_open`으로 오인하지 않도록 수정
- `liquidity-lab`, `auto-run`, `overseas-order-test` 모두 현재 세션에 맞는 해외주식 주문 경로를 사용하도록 보완

### 검증 메모
- KIS 모의서버 응답 확인: `VTTS6036U error: 41070000 모의투자에서는 미국주식 주간거래는 제공하지 않습니다.`
- 텔레그램/CLI/런타임 상태 파일의 사용자 표시 시간을 `YYYY-MM-DD HH:MM:SS KST` 형식으로 통일
- `liquidity-lab` 후보군에 개잡주 방지 필터를 추가: 국내는 저가주/얇은 거래대금/넓은 스프레드 배제, 해외는 저가주/얇은 거래량/넓은 스프레드 배제
- 누적 자동매매 로그를 점검한 결과, 과거 손실의 핵심 원인은 `time_reentry`와 미세한 `time_exit` 반복, 그리고 `stop_loss` 빈도 증가였음
- 이에 따라 자동매매는 `기대수익 > 왕복비용 * 배수`, `기대수익/리스크 비율`을 만족할 때만 진입하도록 강화
- 기본 설정에서 `startup_buy_if_flat=false`, `allow_time_reentry=false`로 보수화
- 오래 남아 있던 `RUNNING` 자동매매 이력은 새 실행 시작 시 `ABORTED`로 자동 정리하도록 개선
- 텔레그램 `stop`/`terminate` 시점에는 해당 세션의 누적 거래 현황, domestic paper 실현손익, 주문 제출/실패 건수, 주요 스킵 사유를 `telegram_control_sessions`에 저장하고 텔레그램으로 즉시 알리도록 개선
- 텔레그램 매수/매도 알림은 `종목 / 동작 / 가격 / 수량 / 핵심 지표 / 시간` 중심의 짧은 포맷으로 축약
- `liquidity-lab`은 해외 1순위 후보만 계속 사던 동작을 수정해, 기존 보유 포지션 중 손절/익절 조건 충족 종목을 신규 매수보다 우선 청산하도록 변경
- 해외 잔고 조회 시 동일 포지션이 거래소별 응답에 중복 노출되더라도 1건으로 정규화하도록 보완
- 텔레그램 컨트롤러의 기본 감시 간격을 `30초`로 낮추고, 다음 실행 시점을 `사이클 종료 후 + interval`이 아니라 `사이클 시작 기준 고정 간격`으로 계산하도록 조정
