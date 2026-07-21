# WORKLOG

## [2026-07-21] CRAN 5,251주 좌초 포지션, 07-14에 발견된 그 자리에서 6일간 방치되어 있었음 — 청산 완료 확인 + 재발 방지 2건

### 요청 배경
"현재까지 로그를 확인하여 분석 및 개선 수행"이라는 포괄적 지시. 서비스 재기동 로그(크래시
재발 여부), 오늘 발생한 서킷브레이커 3회 발동, `trend_filter_lost` 비율 57% 등을 단서로
`journalctl`/`cycle_log`/`event_log`/`broker_order_events`/실시간 잔고조회를 종합 조사.

### 1) 07-16 크래시 수정 효과 확인
지난 항목에서 고친 `_command_loop` 네트워크 예외 가드가 5일간(재기동 이후) 실제로 여러 번
발동했다 — 429 Too Many Requests, 502 Bad Gateway, SSL/TimeoutError 등 다양한 일시적
네트워크 오류를 전부 잡아내 재시도로 넘겼고, 이 기간 동안 서비스 재시작(`NRestarts`)은
0회. 수정 전이었다면 이 중 어느 하나라도 서비스를 죽였을 것 — 크래시 수정이 실전에서
검증됨.

### 2) 오늘 서킷브레이커 3회 발동의 실체 — CRAN 좌초 포지션이 "연속손실"로 오집계됨
`event_log`를 보니 오늘 서킷브레이커가 발동한 세 시각(18:23:15/18:52:32/19:02:35)이 전부
CRAN의 `momentum_loss_cut`/AGBK의 `trend_filter_lost` 실현손익 기록 시각과 정확히 일치했다.
CRAN을 추적한 결과 놀라운 사실을 발견: **`cycle_log`에 CRAN의 `BUY_REAL` 기록이 단 한 건도
없고, 07-20 19:54부터 07-21 19:02까지 `SELL_REAL`만 9건 반복되며 `holding_qty`가
5251 → 5231 → 5227 → 3323 → 2215 → ... 로 서서히 줄어들고 있었다** — 즉 이건 9번의 서로
다른 신규 매수/매도 판단이 아니라, **단 하나의 거대한 좌초 포지션을 여러 사이클에 걸쳐
나눠서 청산하고 있던 것**이었다. 매입가($10.15~10.20)와 현재가가 거의 같아 매번 손실은
수수료만큼(-0.4~0.6%)이었지만, 이 각각의 부분청산이 회로차단기의 "연속손실" 카운터에
독립된 손실로 집계되어 하루에 3번이나 (거짓) 발동한 것.

이 5,251주(≈$53,000) 포지션의 출처를 추적하니 **2026-07-13 CRAN 중복매수 사건(당시 60건+
매수주문이 30분간 반복 제출된 사건, WORKLOG 07-13 항목 참고)의 미해결 잔재**였다.
07-13 조사 당시엔 "실시간 잔고에 CRAN이 안 보여 미체결 주문 더미일 뿐 체결은 안 됐다"고
확인했었는데, **바로 다음날(07-14) WORKLOG에 이미 "CRAN 사건으로 의도치 않게 생긴
5,251주 포지션(매입가 $10.20)"이라고 명확히 기록되어 있다** — 즉 다음날 사이 그 중 일부가
실제로 체결되어 있었다는 뜻이고, 이는 이미 한 번 정정된 사실이었다. 다만 07-14에는 이
포지션이 유발하던 **알림 스팸(`net_profit_below_cost` 반복 발송)만** 고치고, 포지션 자체를
매도하거나 최소한 추적하는 조치는 없었던 것으로 보인다 — 이후 6일간(07-14~07-20) WORKLOG에
CRAN 관련 언급이 전혀 없다. 결국 이 포지션은 누구의 개입도 없이 정확히 6일을 계좌에
잠들어 있다가, 07-20 저녁에야 통상적인 추세/모멘텀 청산 로직이 우연히 이 포지션을 걸어
당겨서야 비로소 청산이 시작됐다. 라이브 잔고 조회로 확인한 결과 현재는 완전히 청산됨
(포지션 0).

### 3) 청산 과정에서 발견한 별도 버그 — "이미 사라진 주문"을 취소실패로 오분류
07-21 13:30~14:50 사이, CRAN의 하루 넘은 미체결 주문에 대해 10분마다 자동취소를 시도했는데
매번 `40320000 모의투자 원주문번호가 존재하지 않습니다`로 실패했다(`stale_live_overseas_
order_cancel_failed`, 8회 반복). 이 오류는 "취소할 대상이 이미 없다"(브로커가 이미 정리함)는
뜻인데, 코드는 이를 일반 실패로 취급해 `REJECTED`로 기록하고 10분 후 똑같은 주문번호로
다시 시도하기를 반복했다 — 절대 성공할 수 없는 재시도를 계속한 것.

### 수정
**(a) 취소실패 오분류 수정** — `telegram_orders.py`에 `_is_order_not_found_error()` 추가,
KIS 오류코드 `40320000`을 감지하면 `REJECTED` 대신 `CANCELED`/`stale_order_already_resolved`
로 기록(실패 알람이 아니라 "이미 처리됨"으로 표시). `message_format.py`에 한글 라벨도 추가.

**(b) 비정상 대형 포지션 조기 감지** — 이번 사건의 핵심 교훈은 "포지션이 6일간 아무도 모르게
방치됐다"는 것. 정상 슬롯 매수로는 절대 나올 수 없는 크기의 포지션을 발견 즉시 텔레그램으로
경보하도록 `liquidity_lab.py`에 `_warn_if_overseas_position_oversized()` 추가 — 계좌 자본/
`slot_max_pct`/최저 허용가로 계산한 "슬롯 매수 최대치"의 3배를 넘는 포지션이 보이면 종목당
1회 경보(`오버사이즈 포지션 감지` 이벤트 + 텔레그램 메시지). 이 CRAN 사례라면 07-14 최초
발견 시점이 아니라 **매 사이클(포지션 로딩 시점)마다 검사**하므로, 같은 상황이 재발하면
6일이 아니라 다음 사이클 안에 알림이 뜬다. 회로차단기의 "같은 포지션 반복 손실을 연속손실로
오집계"하는 문제 자체는 이번엔 고치지 않았음 — 정말 같은 종목에서 반복 손실이 나는 경우와
좌초 포지션 분할청산을 구분하는 로직은 복잡도/리스크 대비 이번 조기경보가 더 직접적인
해결책이라고 판단.

### 검증/배포
회귀테스트 4건 추가(취소실패 재분류 1건, 오버사이즈 경보 발생/미발생/중복억제 각 1건 —
전부 수정 전 코드에서 실패 확인 후 통과 재확인). 전체 스위트 554개 통과.

### CRAN 제외 시 실제 성과 재점검 — `min_hold_before_trend_exit` 30 조정은 효과 있었음
CRAN의 9건을 빼고 다시 보면, 최근 며칠 `trend_filter_lost` 청산의 보유시간이 대부분 12~65분
(과거 5~6분 몰림 현상 사라짐) — 07-16에 12→30으로 늘린 조정이 의도대로 작동 중임을 확인.
승률 자체는 여전히 낮지만(오늘 신규 해외 실거래 7건 중 1승), 표본이 작고 대부분 손실폭이
잡음 수준(-0.04%~-0.26%)이라 추가 조정보다는 며칠 더 데이터를 쌓고 재평가할 필요.

## [2026-07-16] 텔레그램 서비스 크래시 조치 + 반복손절 종목 재감시 정책 개선

### 요청 배경
"서비스 크래시 건에 대하여 조치, 적절한 대상을 타겟하여 감시하는지?(감시종목 선정 방식이
투자방식에 적절한지), 손절 등이 발생한 대상을 계속 감시하는게 맞는지, 혹은 제외하고 다른
대상을 물색하는 것이 올바른지에 대하여 고민할 것"

### 1) 서비스 크래시 원인 확인 및 조치
`journalctl --user -u kinvest-telegram-control.service`로 확인: 17:11:44~46(KST 기준
당일 오후) `_command_loop`의 `self.notifier.get_updates(...)`(텔레그램 롱폴링 GET) 도중
`httpx.ReadError`/`anyio.BrokenResourceError`(일시적 네트워크 단절)가 발생, 이 예외가
`_command_loop` 밖으로 그대로 전파되어 `run()`의 `asyncio.gather(scheduler, command_loop)`
를 깨뜨리고 전체 프로세스가 종료(exit-code 1)됐다. systemd가 5초 후 자동 재시작했지만, 그
사이 스케줄러 루프(매매/청산 감시)도 함께 죽는다.

`_command_loop`에는 이미 `_handle_update` 한 건의 예외가 전체를 죽이지 못하게 하는
try/except가 있었지만("업데이트 처리 중 예외 발생 시 다음 업데이트로 계속 진행" 주석 참고),
그 가드는 `get_updates()` 호출 자체는 감싸지 않고 있었다 — 정확히 이 경로만 뚫려 있던 것.
`get_updates()` 호출을 try/except로 감싸 네트워크 예외 시 3초 대기 후 재시도하도록 수정.
`tests/test_telegram_control.py::test_command_loop_survives_get_updates_network_error` 추가
(수정 전 코드로 되돌려 실제로 크래시 재현 → 원복 후 통과 확인).

### 2) 감시종목 선정 방식이 투자 방식에 적절한지
- 해외: TradingView 상대거래량 급등 스캐너(`tv_min_rel_volume=1.8x`, 가격/거래량/시총 필터,
  `overseas_scan_top_n=25`)로 모멘텀 후보를 매 사이클 갱신. VWAP/RSI/VOL 돌파 전략과 방향이
  일치하는 스크리닝 기준.
- 국내: 고정 후보 7종 + `domestic_dynamic_scan`(당일 등락률/거래량 상위 20종목, 20사이클마다
  재스캔)으로 동적 갱신.
- 직전 거래내역 전수분석(위 항목, 진입 시점 RSI 35~58/breakout거리 거의 0)에서도 확인했듯,
  "타겟 선정" 자체(후보 스크리닝 기준)에서 뚜렷한 결함은 발견되지 않음 — 문제는 선정이 아니라
  진입 후 청산 타이밍(아래 3번, 그리고 직전 `min_hold_before_trend_exit` 조정)이었다.

### 3) 손절 종목 재감시 정책 — "계속 감시" vs "제외 후 새 대상 물색"
데이터 재확인 결과, 사용자의 지적이 맞았다. `cycle_log` 7일치를 종목 단위로 재집계하니
BSBR(54분 간격 2회 연속 `trend_filter_lost` 손실), CCIX/FHN/RQI/WNC도 같은 청산 사유로
재진입 후 다시 손실을 반복한 사례가 다수 확인됨(직전 WORKLOG 항목에서 BSBR 1건만 보고
"쿨다운이 정상 작동 중이라 문제없음"이라 결론 내린 것은 성급했음 — 재진입 자체를 막는
장치는 있었지만, 쿨다운이 끝나면 "같은 종목의 같은 전략 신호"에 무방비로 다시 들어가
같은 패턴으로 또 손실을 내는 경로는 막혀 있지 않았다).

**결론**: 손절/청산 후 해당 종목을 계속 "매수 후보"로 재검토하는 것 자체는 나쁘지 않지만
(추세가 정말 바뀌었을 수도 있으므로 완전 제외는 과함), *연속으로* 손실을 반복하는 종목은
같은 사이클 안에서 재도전하기보다 스캐너가 다른 대상을 찾을 시간을 줘야 한다.

`lab_runtime.py`의 `register_exit_cooldown`에 종목별 연속손실 스트릭(`symbol_loss_streak`)을
추가:
- 손실 청산 시 스트릭 +1, 수익 청산 시 스트릭 리셋(0).
- 스트릭 1회차: 기존 사유별 쿨다운 그대로(예: `trend_filter_lost` 12분).
- 스트릭 2회차(연속 2회 손실, 중간에 수익 없음): 쿨다운 최소 60분으로 상향.
- 스트릭 3회차 이상: 쿨다운 최소 180분(3시간)으로 상향 — 그 시간 동안 스캐너가 다른
  후보로 자연스럽게 넘어가도록 유도.
- `save_event(event_type="symbol_loss_streak_cooldown")`으로 발생 시점 기록(모니터링용).

강제손절(`atr_hard_stop`/하드 스탑)이나 익절 판단에는 영향 없음 — 이 로직은 재진입(신규
매수) 쿨다운에만 적용되고, 보유 중 포지션의 청산 판단 경로(`momentum_policy.py`)와는 무관.
`tests/test_liquidity_lab.py`에 `test_register_exit_cooldown_escalates_on_repeated_losses`
(BSBR 시나리오로 12분→60분→180분 에스컬레이션 확인)와
`test_register_exit_cooldown_streak_resets_after_a_win`(중간에 수익 나면 스트릭 리셋되어
쿨다운이 다시 짧아지는지 확인) 추가. 두 테스트 모두 수정 전 코드에서 실패(`pnl_pct` 인자
자체가 없어 `TypeError`) → 수정 후 통과 확인.

### 검증/배포
전체 테스트 스위트 551개 통과(신규 4건 포함). 이 변경도 전략 파라미터 튜닝의 연장선이라,
실제 효과(반복 손절 빈도 감소)는 이후 거래 데이터로 재확인 필요.

## [2026-07-16] 거래내역 전수분석 — 해외 실거래 11% 승률의 실체는 "타겟 선정"이 아니라 진입 직후 노이즈성 손절

### 요청 배경
"현재까지 거래 내역 보고 문제 파악 및 개선. 전반적으로, 타겟 선정 및 매도 부분에 문제가 많은
것 같음"이라는 지적. `scripts/analyze_trades.py data/trading.db --days 7`로 전수 분석.

### 분석
- 해외 실거래 19건, 승률 11%, 평균 Net -0.549%, 누적 -489,534원. 국내는 5건, 승률 40%,
  평균 Net +1.282%로 오히려 양호.
- 해외 손실의 대부분(14/19건, 74%)이 `trend_filter_lost` 청산에서 발생 — 승률 0%(이 청산은
  `pnl_pct < 0`이 전제조건이라 구조적으로 항상 0%이므로, 문제는 "얼마나 자주 여기서 잘리는가").
- `cycle_log`에서 개별 거래를 직접 대조(진입가/청산가/보유시간/RSI/거래량비/breakout거리):
  - 진입 시점 RSI는 대부분 35~58(중립), breakout_distance_pct는 거의 0에 근접 — 즉 "이미
    과열된 상단을 추격 매수"하는 패턴은 보이지 않음. 타겟 선정 자체는 기술적으로 무리한 진입이
    아니었다.
  - `trend_filter_lost`로 잘린 14건의 보유시간이 거의 전부 5~45분, 그중 다수가 정확히
    5~6분(설정된 최소 보유시간 바로 직후)에 몰려 있었고, 가격 변동폭도 -0.005%~-0.43%로
    사실상 잡음 수준. 반면 유일하게 수익으로 마감된 두 건(`take_profit` +2.5%, `hold=19분`;
    `time_exit_profit` +1.19%, `hold=1100분≈18시간`)은 훨씬 더 오래 버틴 경우였다.
  - Gross(-0.016%)와 Net(-0.549%) 차이가 왕복 수수료/슬리피지(약 0.8%) 그대로다 — 즉
    `trend_filter_lost`가 잡는 손실 자체가 대부분 "가격은 거의 안 움직였는데 수수료만으로
    적자가 되는" 구간이었다.
- **근본원인**: `min_hold_before_trend_exit`(청산 최소 보유 사이클)가 `12`사이클
  (`loop_interval_sec=25` 기준 5분)로, 관찰된 최소 보유시간(5.1~5.6분)과 정확히 일치했다.
  진입 자체가 VWAP/거래량 돌파 시그널이라 "지금 막 이동평균 교차가 일어난 지점"에서 사는
  구조인데, 5분(1개 분봉)만 지나면 노이즈성 되돌림 한 번만으로도 `intraday_trend_up`이
  뒤집혀 청산 조건이 성립해버린다. **이 정확히 같은 진단이 2026-07-10에도 한 번 있었다**
  (`min_hold_before_trend_exit: 5 -> 12`로 이미 한 번 완화했었음, WORKLOG 참고) — 그런데
  6일 뒤 동일한 비율(74%)로 재발한 것을 보면, 그 완화폭(2.4배)이 부족했다는 뜻이다.

### 수정
`config/fixed_config.json`의 `auto_trade.min_hold_before_trend_exit: 12 -> 30`
(25초/사이클 기준 약 12.5분, 5분봉 기준 2.5개 봉 분량). 이 값은 `trend_filter_lost`/
`momentum_loss_cut`처럼 "추세가 식었다"는 판단에만 적용되는 최소 대기시간이며,
`atr_hard_stop`(강제손절)이나 `take_profit`/`time_exit_profit`(익절) 판단은 `hold_cycles`
게이트가 없어 이 변경과 무관하게 그대로 즉시 작동한다 — 즉 진짜 급락을 막는 안전장치는
전혀 늦춰지지 않고, "막 진입한 자리에서 잡음 한 번에 잘리는" 케이스만 완화된다.
`min_hold_before_trend_exit`을 로컬에서 재정의하는 기존 `momentum_policy` 테스트들은 영향
없음(전부 pass 확인).

### 그 외 확인 — "타겟 선정" 자체는 이번 데이터로는 명확한 문제를 못 찾음
BSBR이 54분 간격으로 두 번 진입해 두 번 다 `trend_filter_lost`로 잘린 사례가 있었으나, 재진입
쿨다운(`trend_filter_lost` 후 12분)은 정상 작동 중이었고 단순히 종목 자체가 그날 계속
소폭 등락하는 패턴이었던 것으로 보인다. 진입 시점 RSI/breakout거리 지표상 "무리한 추격매수"
패턴은 확인되지 않았다 — 위 청산 완화가 실제로 효과가 있는지는 다음 거래일 실거래 결과로
재확인이 필요하다(전략 파라미터 튜닝이라 코드 정합성 테스트로는 검증 한계가 있음, 이전
07-10 조정처럼 `--compare-date` 비교로 추후 검증).

**[정정, 같은 날 후속 항목]** 위 결론은 성급했음 — BSBR 1건만 보고 "쿨다운 정상 작동 중이라
문제없다"로 판단했으나, 종목 단위로 전체를 재집계하니 CCIX/FHN/RQI/WNC도 동일 패턴(같은
청산 사유로 재진입 후 재손실)을 보였다. 재진입을 "막는" 장치(쿨다운)는 있었지만 "같은 종목의
연속 손실을 억제"하는 장치는 없었던 것이 실제 원인. 바로 다음 WORKLOG 항목에서 종목별
연속손실 에스컬레이션 쿨다운으로 수정함.

### 검증/배포
`tests/test_config.py`에 새 기본값 assertion 추가. 전체 스위트 548개 통과. git push +
서비스 재기동.

## [2026-07-16] BCC/FG 매도 정지 실사건(held-symbol이 신규진입 필터에 걸리던 버그) + 알림/성능/표기 4건 개선

### 요청 배경
1. 현재 발생하는 "overseas position cap reached" 주문거부가 정상 상황인지, 아니라면 원인 해결
2. `/lab_portfolio` 응답이 느린 이유 확인
3. BCC/FG가 수익 3%+인데 매도 안 하고 대기 중인 게 정상인지, 매도/매수 전략·주문 경로가 정상
   작동하는지 확인. watchlist에 손익 미표시 + "신호=캐시" 표시가 뜨는 원인 분석 및 개선
4. 국내종목 텔레그램 표기를 종목코드가 아니라 한글 종목명으로 변경(긴 이름은 길이 제한)
5. 그 외 발견되는 개선점

### 1) "overseas_position_cap_reached" 오분류 (실제로 정상)
`_send_summary`의 `_IGNORED_SKIP_REASONS`에 `overseas_position_cap_reached`/
`total_position_cap_reached`가 빠져 있어, 동시보유 한도에 정상적으로 도달해 진입을 건너뛴
상태가 "의미 있는 스킵"으로 집계되며 30분마다 `[KIS][거래알림] 동작=주문거부
사유=overseas_position_cap_reached`로 반복 발송되고 있었다. 실제 텔레그램 로그로 확인
(약 30분 간격 반복, `종목=-`라 어떤 종목인지도 알 수 없는 빈 정보). 두 사유를 무시 목록에
추가해 알림 자체를 끔.

### 2) `/lab_portfolio` 응답 지연
두 가지 중복 지연 원인 확인.
- `load_live_virtual_price_lookup`이 이미 전역으로 적용 중인 KIS 클라이언트 페이싱(0.7초/호출,
  2026-07-15 수정)과 별개로 배치당 `asyncio.sleep(1.05)`를 추가로 걸고 있었다 — 이중 페이싱,
  제거.
- `build_portfolio_message`는 이미 `last_report.watch_targets`/`repository.list_lab_symbol_
  state`에서 최근(대개 20초 이내) 가격을 읽어와 표시하는데, `send_portfolio_message`는 이
  캐시가 신선한 상태(루프 실행 중 + 상태 지연 임계치 이내)에서도 매번 보유 가상종목 전부에
  대해 실시간 시세를 다시 조회하고 있었다. 정확도 개선은 거의 없이 종목 수에 비례한 지연만
  추가하던 것 — 캐시가 신선하면 재조회를 건너뛰도록 수정(루프 중지/데이터 오래됨일 때는 그대로
  실시간 재조회, 안전장치 유지).

### 3) BCC/FG 매도 정지 + watchlist "신호=캐시" 실사건 — 진짜 근본원인
실시간 조회로 BCC/FG의 watch_target note에 `|stale_signal_cache`가 붙어 있는 것을 확인하고
추적. `scan_overseas()`가 각 후보에 `_overseas_speculative_reasons`(저가/얇은거래량/넓은
스프레드/얇은거래대금 — 신규 매수용 품질 필터)를 **held 여부와 무관하게** 적용하고 있었다.
BCC/FG는 실시간 시세의 거래량/스프레드가 순간적으로 이 필터에 걸려 `quote_results`에서
제외됐고, 그 사이클엔 `_signal_cache`에 값이 안 남아 다음 watch target 생성 시
`signal_snapshot is None` -> 마지막으로 캐시된(오래된) 신호로 청산 판단을 하는 폴백 경로를
탔다. 화면엔 "신호=캐시"로 표시되고, 정작 가격은 계속 움직이는데 그 움직임을 반영한 청산
판단(RSI/MA 기반)은 멈춰 있었던 것 — 3%+ 수익에도 매도가 안 나간 이유. `_overseas_signal_
suppression_reason`은 이미 held 종목을 예외 처리하고 있었는데(2456줄), 바로 옆의
`_overseas_speculative_reasons` 필터는 예외가 없었던 비대칭. held 종목을 이 필터에서 예외
처리하도록 수정. 국내 `scan_domestic()`도 동일한 비대칭이 있어(`_domestic_quote_speculative_
reasons`/`_domestic_speculative_reasons`) 같은 원리로 held 종목 예외 처리 추가(직접 재현된
실사건은 아니지만 정확히 같은 구조적 결함이라 함께 수정). README 핵심 설계 원칙 7번으로
명문화.

### 4) watchlist 표현 개선
- "신호=캐시"가 무슨 뜻인지 알기 어렵다는 지적에 따라 "신호=갱신지연(직전값 사용)"으로 변경.
- 보유 중인데 실시간 손익 조회가 아직 안 된 경우 `보유=N주` 표시 자체가 통째로 사라지던 것을
  고쳐, 손익만 `손익=조회중`으로 표시하고 보유수량은 항상 보이게 함.

### 5) 국내종목 표기를 코드 우선에서 한글 이름 우선으로 변경
`005930(삼성전자)` -> `삼성전자(005930)`로 순서 변경(`format_domestic_symbol_label`,
`message_format.py`, 신규 공용 헬퍼). 이름이 12자 초과 시 말줄임표로 축약. 추가로, 보유
종목의 이름이 그날 거래량/등락율 순위 풀에 없으면 이름 자체를 못 찾던 문제도 발견 —
`get_balance()` 응답에 이미 있는 `prdt_name` 필드를 이름 맵에 직접 채우도록 `_load_domestic_
positions`를 수정해, 순위권 밖의 보유종목도 항상 한글명이 표시되게 함.

### 검증/배포
5개 항목 각각 회귀 테스트 추가, 전부 `git stash` 후 fail 확인 -> `stash pop` 후 pass 확인.
전체 테스트 548개 통과. git push + 서비스 재기동.

## [2026-07-15] 시장별 동시보유 한도 완화 + 국내매수 3차 재조사 — 소수점 가격 수정으로도 여전히 100% 실패, 근본원인 미해결로 재오픈

### 요청 배경
"동시보유 한도 8이 특별한 이유 없다면 완화할 것", "국장/미장 중 실제 거래 불가능한 시간대는
보유중이라도 감시항목에서 제외할 것", "현재까지 발생한 오류도 함께 검토·해결할 것" 세 가지 요청.

### 1) 동시보유 한도 완화
`max_concurrent_overseas_orders`/`max_concurrent_domestic_orders`가 `8`로 설정된 배경을
git 히스토리로 추적: 2026-07-10 해외 손실 집중(RSI/VWAP 단독 진입) 분석 후 `20 -> 8`로 낮춘
실제 근거 있는 조정이었다. 하지만 바로 다음날(07-11) `max_concurrent_total_positions=10`
(자본 기준 실질 총 노출 상한, `slot_entry_pct=0.10` x 10슬롯 = 자본 100%)이 신설되어 두
시장 합산 노출을 이미 별도로 캡핑하고 있고, 같은 날 여러 진입-품질 가드
(`overseas_block_standalone_vwap/rsi/vol` 등)도 함께 추가되어 손실 원인이었던 약한 단독
신호 진입 자체를 차단하는 구조로 바뀌었다. 즉 시장별 캡 `8`은 이미 상위의 총량 캡(`10`)보다
낮아 실질적으로 이중 안전장치가 아니라 시장 간 슬롯 배분을 억지로 8:8로 쪼개는 역할만 하고
있었다. `max_concurrent_overseas_orders`/`max_concurrent_domestic_orders`를 `8 -> 10`으로
올려 총량 캡(`10`)과 일치시킴 — 실질 자본 노출은 총량 캡이 그대로 막아주므로 리스크는
늘지 않고, 한 시장이 정당하게 더 많은 기회를 잡을 때 다른 시장의 몫만큼만 남기던 인위적
제한이 없어짐.

### 2) 시장별 실제 거래 가능 시간 감시 제외
`market_sessions.py`로 실사간 확인: 국내는 이미 `is_krx_regular_session()`이 `False`인 시간엔
보유종목까지 포함해 `domestic_positions=[]`로 완전히 감시 대상에서 빠지고 있었다(방금
실시간 조회로 재확인: 장 마감 후 감시항목 8건 전부 해외, 국내 0건). 해외는 `us_open`이
"daytime/premarket/regular/aftermarket"을 모두 포함해 모의계좌가 실제 주문 불가능한
확장시간대에도 보유(실+가상)종목을 계속 감시하지만, 이는 조기 손절 신호를 "가상매도"로
즉시 기록해뒀다가 정규세션이 열리면 실제 매도로 정산하는 기존 안전장치(`_reconcile_pending_
virtual_sells`)가 의도적으로 의존하는 동작이라 확인 후 사용자에게 트레이드오프를 보고,
"실거래 장 시간" 기준은 현재의 넓은 세션 정의(확장시간 포함)를 의미한다는 확인을 받아
**현행 유지** — 코드 변경 없음.

### 3) 국내매수 100% 실패 재조사 — 어제 수정이 불충분했음을 확인
어제(2026-07-14) `submit_price = int(buy_price)`로 소수점 가격 문제를 고쳤다고 기록했으나,
오늘 실사간 DB 재조회 결과 **국내매수가 수정 배포(07-14 20:36 UTC) 이후에도 오늘 하루
36건 중 36건(100%) 전부 동일한 `IGW00007`로 계속 실패**하고 있었다. 어제의 결론이 성급했음을
인정하고 재조사:

- 실패 당시 실제 로그에 기록된 종목코드/수량/가격(예: `233740` qty=91 price=7835,
  `229200` qty=50 price=14320, `005930` qty=2 price=281000)을 그대로 `client.place_cash_order`로
  재현 호출(장마감 후, 모의계좌라 안전) → 매번 깨끗하게 `40570000 모의투자 장시작전입니다`만
  반환됨. 즉 정확히 같은 바디가 스키마 파싱은 통과해 시장시간 검사까지 도달함 —
  "바디가 깨져 있다"는 설명과 맞지 않음.
- `/tmp/open-trading-api`(KIS 공식 샘플 저장소, 로컬에 이미 클론되어 있음)의
  `examples_llm/domestic_stock/order_cash/order_cash.py`(2025-01-12, 우리와 동일한
  `VTTC0012U`/`VTTC0011U` tr_id 사용)와 현재 `client.py`의 `place_cash_order` 바디를
  필드 단위로 대조 — `CANO/ACNT_PRDT_CD/PDNO/ORD_DVSN/ORD_QTY/ORD_UNPR/EXCG_ID_DVSN_CD/
  SLL_TYPE/CNDT_PRIC` 9개 필드가 완전히 일치. 바디 자체는 공식 샘플과 동일한 최신 스펙.
- 실패 가격들이 KRX 호가단위(틱)를 어기는지 검토 — 종목 대부분이 ETF/ETN
  (`069500/229200/122630/233740/379800/360750/0167A0/0162Z0`)로 이들은 일반주식 틱 테이블이
  아니라 5원 단위의 별도(ETF/ETN) 틱을 쓰므로 재계산하면 실패 가격 전부 유효한 틱 — 틱
  위반 가설 기각.
- 실시간 잔고 조회로 가용 예수금(`dnca_tot_amt`) 약 7.9백만원 확인 — 슬롯 예산(10%)이 예수금
  부족으로 0이 되는 경우도 아님.

결론: 어제 수정(정수 가격 캐스팅)은 KRW 소수단위가 없다는 점에서 여전히 올바르고 유지해야
하지만, **국내매수 100% 실패의 진짜 원인은 아니었거나 원인 중 하나일 뿐 전부는 아니다.**
바디 형식·틱단위·가용자금을 모두 배제했으므로 남은 가설은 모의투자(VTS) 계좌의 실시간
매칭엔진 단계에서 발생하는 비-바디 문제(예: 계좌 자체의 특정 제약, 또는 `EXCG_ID_DVSN_CD`
값이 실전과 달리 모의투자에서 세션 오픈 이후에만 검증되는 방식의 계정별 이슈)로 좁혀지나,
장 마감 후에는 시장시간 검사에서 조기 반환되어 그 다음 단계 검증을 재현할 방법이 없어
**오늘은 확정하지 못함**. 다음 국내 정규장(09:00 KST)에 실시간으로 바디 변형(예:
`EXCG_ID_DVSN_CD`/`CNDT_PRIC` 제거, `ORD_DVSN="01"` 시장가 매수 시도 등)을 하나씩 시도해
원인을 좁혀야 한다 — **근본원인 미해결 상태로 재오픈**.

### 배포
`config/fixed_config.json`의 `max_concurrent_overseas_orders`/`max_concurrent_domestic_orders`를
`8 -> 10`으로 변경, `tests/test_config.py` 기대값 갱신. 전체 스위트 537개 통과. git push +
서비스 재기동. 국내매수 원인 조사는 코드 변경 없이(성급한 재수정 방지) 재오픈 상태로 문서화만.

## [2026-07-15] 국내매수 100% 실패, 2차 근본원인 — 소수점 가격 문자열이 매번 거부됨

### 배경
"주문거부 건이 지속 발생하는 점, 주문 자체가 활발하게 일어나지 않는 점"을 다시 점검해 달라는
요청. 실시간 로컬 DB(`data/trading.db`)를 직접 조회해(깃에 올라간 CSV 스냅샷이 아니라) 페이싱
수정 배포(05:13 UTC) 이후 4.5시간을 재확인.

### 발견
국내매수(`VTTC0012U`)가 페이싱 수정 이후에도 9건 중 9건(100%) 계속 `IGW00007`로 실패 중이었다.
이번엔 시도 간격이 5~8분으로 넓어(직전 페이싱 버그와 무관), 순수 레이트리밋이 아닌 별개의
문제임을 확인. `lab_domestic_orders.py`의 매수 경로(`place_test_order`)를 다시 추적한 결과:
`buy_price = float(...)` 이후 `submit_price = buy_price`로 **정수 변환 없이** 그대로
`place_cash_order`에 넘기고 있었다. `client.py`는 `"ORD_UNPR": str(price)`로 그대로
문자열화하므로, 가격이 `80000.0`처럼 소수점을 포함한 문자열로 제출되고 있었다 — 원화는 소수
단위가 없는데도. 매도 경로(`_sell_order_submit_spec` → `submit_price = int(...)`)는 이미 정수
캐스팅을 거치고 있어 매도만 정상 동작해 온 것과 정확히 대응된다.

**참고**: 2026-07-14 조사에서 이 정확한 float-vs-int 가설이 한 번 제기됐다가 "이 float 코드가
과거 정상 동작 기간(07-08~07-10) 이전부터 있었다"는 git blame 근거로 기각된 바 있다. 그
추론은 "그 시점에 이 경로가 실제로 실행됐다"는 근거는 아니었으므로, 버그를 배제하는 증거가
되지 못했다 — 오늘 실사건으로 재현·확정.

### 수정
`place_test_order`의 `submit_price = buy_price`를 `submit_price = int(buy_price)`로 변경.
회귀 테스트 추가: 더미 클라이언트로 실제 `place_cash_order` 호출에 전달되는 `price` 값이
`int` 타입인지 직접 검증(수정 전 코드로 되돌리면 `isinstance(80000.0, int)`가 `False`로 실패
재현 확인).

### 그 외 확인 — "주문이 활발하지 않다"
해외는 실제로는 활발했다: 오늘 새벽(05:46~06:25 KST) TV 스캐너 수정 이후 정상화된 풀에서
BCC/CLM/CPRX/FG/UGP/WFC 6개 신규 가상포지션이 40분 만에 열렸고, 실보유(CLSK/CRAN) 2개와
합쳐 `max_concurrent_overseas_orders=8` 한도에 도달해 이후 대기 중인 것으로 확인 — 버그가
아니라 설계된 동시보유 한도. 국내는 매수가 100% 실패해 왔으니 체감상 "활발하지 않다"로
보인 것이 당연했고, 위 수정으로 다음 국내 정규장(내일 09:00 KST)부터 정상화될 것으로 기대.

### 검증/배포
`tests/test_liquidity_lab.py` 신규 1건 포함 도메스틱 매수 테스트 10개 전부 통과(fail→pass 확인),
전체 스위트 537개 통과. git push(자동 gitlog 업로드 커밋과 병합) + 서비스 재기동. 국내장이
마감된 시각이라 오늘 라이브로 실매수 성공까지는 확인하지 못함 — 다음 정규장에서 확인 필요.

## [2026-07-15] 국내매수 100% 실패 재발 실사건 — 페이싱이 인스턴스별로 갇혀있던 진짜 근본원인

### 배경
"현재 거래내역 확인하여 문제 해결"을 요청받아 재기동(2026-07-14 20:36 UTC) 이후 8시간치
`api_call_log`를 전수 조회.

### 발견
- `EGW00201`(초당한도초과)/`IGW00007`(전문바디오류) 실패가 거의 모든 엔드포인트에서 30~42%,
  국내매수(`VTTC0012U`)는 **0/32 (100%) 전부 실패** — 어제 배포한 0.3초 페이싱 수정이 사실상
  전혀 듣지 않고 있었음.
- 원인: `_throttle`의 `_rate_limit_lock`/`_last_request_at`이 `self.`(인스턴스) 속성이었다.
  그런데 `_run_cycle`은 **매 사이클마다 새 `KisRestClient()`를 생성**하고, `/lab_portfolio`·
  `/lab_status`·`/lab_orders`·gitlog 업로드 등 텔레그램 명령 핸들러도 각자 **별도의 임시
  `KisRestClient`**를 열어 메인 루프와 동시에 호출한다. 인스턴스별 페이싱은 그 객체를 통한
  호출끼리만 간격을 보장하므로, 서로 다른 인스턴스들이 동시에 KIS 계정 전체의 초당 한도를
  나눠 쓰지 못하고 각자 독립적으로 0.3초 간격을 유지한 채 경합해 실제 합산 호출률이 한도를
  계속 초과했다. 실시간으로 확인한 현재 사이클의 국내매수 시도도 동일 오류로 실패 중이었다.

### 수정
- `client.py`: `_rate_limit_lock`/`_last_request_at`/`_min_request_interval_sec`을 클래스
  속성으로 변경 — 프로세스 내 모든 `KisRestClient` 인스턴스가 하나의 페이싱 시계를 공유한다.
  `tests/test_client.py`에 "별도 인스턴스 두 개도 같은 시계를 공유하는지" 검증하는 회귀 테스트
  추가(수정 전 코드로 되돌려 `AttributeError`로 실패 재현 확인).
- 배포 후 라이브 검증(0.5초): 분당 실패율이 초기 40%대에서 등락하며 8분 넘게 수렴하지 않아
  KIS의 초당 한도가 순수 슬라이딩 윈도우가 아니라 달력초 단위 버킷일 가능성을 의심, 0.6~0.7초
  구간이 이론상 더 안전하다고 판단해 0.7초로 상향.
- 별도의 통제된 테스트(가짜 HTTP 클라이언트, 5개 인스턴스에서 20개 요청을 동시에 발사)로
  `_throttle` 자체가 매번 정확히 0.7초 간격을 강제하는지 직접 검증 — 최소/최대 간격 모두
  0.702초로 확인, 페이싱 로직 자체에는 결함이 없음을 확인.
- 0.7초 배포 후 15분 관찰: 개별 시세조회 호출 레벨에서는 여전히 ~20% 안팎의 `EGW00201`
  잔여 실패가 있었으나(이미 있는 3회 재시도 로직이 대부분 흡수), **실제 영향 지표는 완전히
  깨끗함** — 이 구간 동안 서킷브레이커 발동 0건, 국내매수 신호 발생 시도 자체가 없었음(즉
  전날처럼 "시도→실패→CB 발동"이 반복되는 패턴이 재현되지 않음). 잔여 시세조회 노이즈는 KIS
  모의투자(VTS) 서버 자체의 특성(다른 이용자와 공유하는 상대적으로 불안정한 샌드박스
  인프라)일 가능성이 높고, 클라이언트 측 페이싱만으로는 완전히 없앨 수 없는 영역으로 판단—
  이미 있는 재시도 로직이 흡수하는 선에서 두고, 실거래(주문 제출/서킷브레이커)에 영향이
  없는지를 계속 지켜보는 것으로 충분하다고 결론.

### 검증/배포
`tests/test_client.py` 11개 전부 통과(수정 전 코드로 되돌려 실패 재현 후 복구), 전체 스위트
536개 통과. git push(원격의 자동 gitlog 업로드 커밋과 병합 후) + 서비스 재기동 2회(0.5초 →
0.7초) + 15분 라이브 관찰.

## [2026-07-14] 감시종목 공백 실사건(TV 스캐너 버그) + 초기설계 대비 구조표류 3차 전수감사 + README 재구성

### 배경
"감시종목이 왜 보유종목 둘뿐이고 새 후보가 안 보이는지"와 "몇십 번의 자동개선을 거치며 구조가
꼬인 것 같으니 초기 설계 목표부터 지시한 구조들이 잘 유지됐는지 전수조사 후 대대적 개선 + README
재작성"을 요청받음.

### 조사 1: 감시종목 공백 — TV 스캐너 실사건
`runtime_state.json`에 감시종목이 보유종목(CRAN, MSEX) 2개뿐이었음. `event_log`는 TV 스캔이 매번
30종목 풀을 정상 반환한다고 기록하고 있어 처음엔 "새벽 프리마켓 저유동성" 정도로 의심했으나, 실제
KIS API로 풀 내 종목의 시세를 직접 조회해보니 풀 자체가 가비지였다: OTC 장외 페니주(`SNEJF`,
`PCRHY`, `DNKEY` 등 5글자+Y 전형적 ADR 티커)와 우선주(`APO/PA`, `HPE/PC`)가 다수 포함돼 있었고,
이들은 KIS 시세 조회에서 빈 응답이거나 거래량 0으로 하위 유동성 필터에서 거의 전부 걸러졌다.

원인: `tv_scanner.py`의 `_parse_tv_symbol`이 TradingView 응답의 `d`/`name` 컬럼(거래소 정보가
전혀 없는 맨 티커)을 파싱하면서, 콜론(`:`)이 없으면 무조건 `NASD`로 간주하고 있었다. 실제 거래소
정보는 각 행 최상위의 `s` 필드(예: `"OTC:SNEJF"`)에만 있는데 이 필드를 아예 읽지 않았다. 원본
TradingView 응답을 직접 재현 요청해 `s`/`d` 필드 구조를 확인하고 확정.

### 수정 1
- `tv_scanner.py`: `s` 필드 기반으로 거래소를 파싱하도록 변경(더 이상 콜론 없으면 NASD로 추정하지
  않음). `typespecs`에 `preferred`/`warrant`/`right`/`unit`이 포함된 행 제외. 필터링으로 줄어드는
  분량을 보정하기 위해 요청 range를 `top_n*4`로 확대.
- `tests/test_tv_scanner.py`: 기존 테스트 자체가 "d 컬럼에 EXCHANGE:SYMBOL이 들어있다"는 잘못된
  가정으로 작성돼 있어(버그가 발견되지 않은 이유) 실제 API 응답 형태로 전부 재작성 + 신규 케이스 2개.
- 실제 KIS API로 라이브 대조(수정 전: 가비지 다수 / 수정 후: 전부 유효 NASD/NYSE 공통주)로 확정.

### 조사 2: 초기 설계 목표 대비 구조 표류(drift) 3차 전수감사
서브에이전트로 `WORKLOG.md` 전체(4,276줄) + `git log --oneline --all`(253개 커밋) 기준 설계
의도 타임라인을 재구성하고, 병행해서 (a) 판단-제출-차단 재발 패턴이 다른 곳에 더 있는지, (b) 국내
동적스캔이 TV류 버그를 공유하는지, (c) 설정/텔레그램 명령어 정합성을 각각 재검사.

결론: 핵심 구조(`auto-run`/`liquidity-lab` 공존, `momentum_policy` 공유, 시장별/합산 한도 임의조정
금지 원칙)는 모두 유지되고 있었음. 유일한 실제 표류는 이미 전날(2026-07-14 1차) 발견·수정한
"이중신호 BUY 우회" 버그였고(2026-07-02 `a75e3f5`가 세운 "watch_target 도달 시 이미 검증됨"이라는
전제가 이후 리팩터링으로 조용히 깨진 사례), 추가 조사에서는 새로운 판단-제출-차단 인스턴스나 국내
스캔 버그는 발견되지 않음(기존 안전장치가 정확히 작동 중임을 확인).

설정/텔레그램 감사에서는 아래를 발견·수정:
- `liquidity_lab.overseas_candidates`가 완전히 죽은 설정이었음 — TV 스캐너가 주 소스가 된 이후
  아무도 읽지 않아 TV 장애 시 기대되는 정적 폴백 역할을 못 하고 있었음.
- 일일손실 서킷브레이커의 `operating_capital_krw` 폴백 리터럴이 실제 기본값(5천만원)과 다른
  500만원으로 박혀 있었음(현재는 항상 정상 설정돼 있어 잠재 위험만 있던 상태).
- `overseas_exit_mid_mismatch_pct`/`overseas_exit_price_shock_pct`/`overseas_exit_price_shock_confirm_pct`
  3개 값이 데이터클래스/JSON에 없이 코드에만 하드코딩돼 있어 조정 불가능했음.
- `/lab_cb_reset`의 README 설명이 실제 동작(일일손실 정지는 해제 안 함)보다 범위를 과대 서술.

### 수정 2
- `liquidity_lab.py`: `_refresh_overseas_dynamic_pool`에 정적 폴백 분기 추가 — TV 스캔이 비고
  수동 relist도 없을 때 `config.liquidity_lab.overseas_candidates`가 있으면 그것으로 대체하고,
  둘 다 없을 때만 기존처럼 relist 요청 알림으로 넘어간다.
- `config.py`/`fixed_config.json`: 위 3개 청산 가드 값을 정식 필드로 추가. `inverse_etf_symbols`/
  `leveraged_etf_symbols`가 `liquidity_lab` JSON 섹션을 공유한다는 사실을 주석으로 명시.
- `lab_risk.py`: `operating_capital_krw` 폴백 리터럴을 실제 기본값(5천만원)과 일치시킴.
- `README.md`: `/lab_cb_reset` 설명 정정.
- 검증: 모든 수정을 `tests/test_tv_scanner.py`, `tests/test_liquidity_lab.py`,
  `tests/test_overseas_scan.py`, `tests/test_config.py`, `tests/test_lab_risk.py`에 회귀 테스트로
  추가하고 수정 전 코드로 되돌려 실패 재현 확인 후 복구. 전체 스위트 535개 통과.

### README 재구성
"구조가 꼬인 느낌"이라는 지적에 맞춰 문서 자체도 정리:
- 최상단에 [핵심 설계 원칙](README.md#핵심-설계-원칙-반드시-유지) 섹션 신설 — 리팩터링 중 실제로
  깨졌던 원칙들(판단-제출-차단 금지, 매수판단 단일 권위 경로, auto-run/liquidity-lab 의도된 공존,
  한도 임의조정 금지, 동적풀 무음 공백 금지, public repo 계좌정보 금지)을 앞으로도 지켜야 할
  체크리스트로 명문화.
- `Liquidity Lab` 섹션에 뒤섞여 있던 "현재도 유효한 동작 규칙"과 "이미 끝난 사건의 원인 분석
  narrative"를 분리 — 사건 서술은 전부 [부록: 주요 인시던트 히스토리](README.md#부록-주요-인시던트-히스토리)로
  이동하고, 본문에는 지금 살아있는 규칙만 남김.
- 오늘 조사 내용(TV 스캐너 버그, 3차 감사 결과)을 부록에 추가.

### 배포
git push + `kinvest-telegram-control.service` 재기동 + 텔레그램 보고.

## [2026-07-14] 국내매수 주문거부 실사건 조사 + 전략로직 2차 전수감사(치명적 버그 1건)

### 배경
직전 배포 재기동 로그에서 국내매수(VTTC0012U)가 반복 실패하며 서킷브레이커가 계속 발동 중인 것을
발견해 원인 조사를 요청받음. 추가로 "전반적인 전수조사, 특히 전략/매매로직의 논리적 오류"에
집중해 재점검하되 "오류가 없으면 억지로 판단하지 말 것"이라는 조건이 붙음.

### 조사 1: 국내매수 반복 주문거부
`api_call_log`/`broker_order_events`를 직접 대조: 국내 매도(VTTC0011U)는 정상, 매수(VTTC0012U)만
당일 71건 중 71건 전부 실패(`EGW00201` 초당한도초과 또는 `IGW00007` 전문바디오류). 같은 종목이
성공/실패를 오가는 것으로 보아 요청 바디 자체 문제는 아니었고, 실패 시점마다 **서로 다른 종목의
매수 주문이 약 240ms 간격으로 연달아 제출**되고 있었다 — 한 사이클에 여러 종목을 순차 제출하는
구조(`max_concurrent_domestic_orders`)에 오늘 추가한 미체결조회 호출까지 겹치며 KIS 초당 호출
한도를 자체적으로 초과한 것.

### 수정 1
- `src/kinvest_trade/client.py`: `KisRestClient`에 모든 실제 호출 전에 최소 0.3초 간격을 강제하는
  전역 페이싱(`_throttle`, `asyncio.Lock` 기반)을 추가. 기존의 "거부 응답을 받은 뒤 재시도"가 아니라
  애초에 폭주를 만들지 않도록 사전 예방.
- 검증: `tests/test_client.py`에 페이싱 회귀 테스트 추가, 수정 전 코드로 되돌려 실패 재현 확인.

### 조사 2: 전략/시그널 로직 2차 감사
직전 라운드에서 감사하지 않았던 `lab_watch.py`(1294줄, 통합 감시종목·매수/매도 대상 선정),
`technical_signals.py`, `adaptive_params.py`, `lab_positions.py`, `lab_risk.py`를 서브에이전트로
정밀 재검토. 두 건 보고됨:

1. **RSI 계산 순서 의혹 — 검증 결과 오탐(버그 아님)**: `technical_signals.py`가 RSI만 시간순으로
   뒤집지 않고 원본(최신순) 배열을 그대로 `compute_rsi`에 넘긴다는 지적. 실제로 순수 교과서식
   RSI(진짜 시간순 등락으로 직접 계산한 값)와 대조 계산해보니 **현재 코드가 이미 정답과 일치**했고,
   "수정안"대로 시간순으로 뒤집어 넣으면 오히려 RSI가 반전(예: 77.8 → 22.2)되는 것으로 확인됨.
   `compute_rsi` 내부 인덱싱(`closes[:period]`를 curr로, `closes[1:period+1]`를 prev로 사용)이
   최신순 입력을 전제로 설계돼 있어, 호출부가 옳았다. **수정하지 않음** — 문제가 없는 것을 억지로
   고치지 않는다는 원칙에 따름.
2. **[치명적] 매수 판단 이중구조로 인한 안전장치 우회 — 실제 버그, 수정함**: `lab_watch.py`의
   `build_watch_target_status`가 매수 여부를 두 개의 독립된 판단으로 계산: 정식
   `PriorityStrategyManager.evaluate()`(`strategy_result.signal`)와, 별도의 모멘텀 휴리스틱인
   `evaluate_entry_setup`/`derive_watch_state`. `strategy_result.signal != "BUY"`이지만
   `derive_watch_state`가 독립적으로 `"BUY"`를 반환하면, 전략차단(`_entry_strategy_block_reason`)·
   유동성차단(`_entry_liquidity_block_reason`)·VWAP/RSI 확인대기·**재진입 쿨다운
   (`_cooldown_remaining_minutes`)**을 전혀 거치지 않고 그대로 `action_bias="BUY"`가 나갔다.
   `select_domestic_buy_targets`는 `action_bias=="BUY"`만 보고 무조건 매수 후보로 채택(재검증 전혀
   없음), `select_overseas_buy_targets`는 전략차단만 부분적으로 재검증할 뿐 유동성차단/쿨다운은
   재검증하지 않아 해외도 새어나갈 수 있었다. 즉 **손절 직후 재진입 쿨다운 중인 종목이 이 경로로
   즉시 재매수될 수 있는 실제 우회로**였다. 같은 파일의 "stale signal cache" 분기는 정확히 이
   이중신호 위험을 이미 인지하고 항상 WAIT로 억제하고 있었는데(선행 커밋에서 의도적으로 만든 안전장치),
   정작 평시(live) 경로에는 동일한 처리가 빠져 있었다.

### 수정 2
- `src/kinvest_trade/lab_watch.py`: `build_watch_target_status`의 live 경로에서
  `strategy_result.signal != "BUY"`인데 `derive_watch_state`가 단독으로 `"BUY"`를 반환하는 경우,
  기존 stale-cache 분기와 동일하게 항상 `action_bias="WAIT"`로 억제(`signal_state`는 그대로 노출해
  진단은 가능하게 유지, note에 `strategy_unconfirmed_buy_blocked` 부기).
- 검증: `tests/test_liquidity_lab.py`에 회귀 테스트 추가 — 수정 전 코드로 되돌리면 실제로
  `action_bias=="BUY"`가 새어나가는 것을 직접 재현·확인.

### 검증
- `python3 -m pytest -q` → `530 passed`
- 다른 파일(`adaptive_params.py`/`lab_positions.py`/`lab_risk.py`)은 정밀 검토 결과 진짜 오류를
  찾지 못해 그대로 두었다(서킷브레이커 일별 리셋은 KST 기준으로 정확히 동작, 포지션 평단가/부분청산
  회계도 정확함을 확인).

## [2026-07-14] git log 기반 전수 감사 — 로직 버그 9건 일괄 수정

### 배경
사용자 요청: "git log 올린 것을 바탕으로 문제사항 수정 및 개선 진행할 것, 추가로 전체 전수확인
진행하여 로직상 잘못된 부분을 전면 수정할 것. 이후 거래 내용 리셋할 것." 최근 커밋 이력(CRAN 사건,
자동취소 매칭버그, 미체결 정체, 반복알림, time_exit_profit 등)에서 반복적으로 나타난 버그 패턴 —
①판단-시도-차단 이중구조 ②자릿수/단위 불일치 ③조회 필터·페이지네이션 누락으로 인한 안전장치 무력화
④알림 폭탄 ⑤조용한 예외 삼킴 — 이 코드베이스 전체에 더 있는지 여러 서브에이전트로 병렬 심층 감사를
진행했다(전략판단/주문실행/liquidity_lab 코어 2분할/텔레그램 컨트롤/repository·config·client, 총 6개
영역).

### 발견 및 수정 (전부 회귀 테스트로 재현 확인 후 수정)
1. **[치명적] 서킷브레이커 발동 중 매 사이클 크래시**: `liquidity_lab.py` `_run_cycle`에서
   `_is_trading_halted()`가 True인 분기가 `domestic_reject_halted`/`overseas_reject_halted`를
   할당하지 않는데, 몇 줄 뒤에서 무조건 참조해 `UnboundLocalError` 발생. 서킷브레이커
   쿨다운(기본 30분) 내내 이 예외가 반복되며 그 사이클의 손절/익절 처리가 전부 스킵됐다. 두 변수를
   halted 분기에서도 `False`로 초기화하도록 수정.
2. **[치명적] 텔레그램 메뉴 더블탭 → 서비스 전체 크래시 + 재크래시 루프**: `_handle_menu_callback`의
   `edit_message` 호출이 예외를 잡지 않아, Telegram의 "message is not modified"(더블탭 시 흔함) 같은
   응답도 그대로 전파 → `_command_loop` → `run()`의 FATAL 핸들러까지 올라가 프로세스 전체가
   종료됐다. 게다가 update_offset은 처리 성공 후에만 저장되므로, 재시작해도 같은 업데이트가 다시
   전달되어 크래시가 반복될 수 있었다. `edit_message` 호출을 `contextlib.suppress`로 감싸고,
   `_command_loop`가 업데이트 1건 처리 실패를 로그만 남기고 다음으로 넘어가도록(오프셋은 항상 저장)
   강화.
3. **재진입 쿨다운 무력화**: `auto_trader.py`의 `cooldown_block`이 `entry_setup.ready`가 False일 때만
   검사되어, 매수신호가 이미 준비된 경우엔 `force_reentry_after_cycles` 쿨다운이 사실상 전혀 작동하지
   않았다. 무조건 먼저 검사하도록 수정.
4. **반복 스킵 알림 쿨다운(직전 커밋)이 비주력 시장에서 무력화됨**: `_build_action_summary`가
   `report.primary_market`과 일치하는 시장의 주문만 대표로 뽑다 보니, 실제 스킵이 다른(비주력)
   시장에 있으면 심볼 정보 없는 제네릭 dict로 빠져 알림 문구도 부정확하고 쿨다운 키도 매 사이클
   회전하는 대표종목을 따라가 무력화됐다. 두 시장 모두에서 "의미 있는" 스킵을 우선 찾도록 재구성.
5. **`/lab_watchlist` 통화 표기 오판**: 가격이 1,000 이상이면 원화, 아니면 달러로 추측했는데, 저가
   미국주식/고가 국내주식에서 표기가 뒤바뀔 수 있었다. 항목 자체의 `market` 필드를 사용하도록 수정.
6. **`/lab_reset_all` 캐시 초기화 누락**: 반복알림 쿨다운, 해외신호 실패쿨다운, 손절확인 가드,
   가격쇼크 가드, 마지막 보유종목 캐시가 초기화 목록에서 빠져 있었다. 목록에 추가.
7. **잔고 조회 실패 시 "보유종목 없음"으로 오판**: `_load_domestic_positions`/`_load_overseas_positions`가
   API 실패 시 빈 리스트를 반환해, 실제 보유종목이 그 사이클 동안 손절/익절 감시에서 완전히
   빠질 수 있었다. 실패 시 직전 캐시로 대체(해외는 거래소 단위로 개별 대체)하고 이벤트를 남기도록 수정.
8. **`momentum_loss_cut`의 조기 확인 조건**: 분봉 장기이평(`minute_ma_slow`)이 아직 준비되지 않은
   구간에서 `price_below_ma`가 `price < price + 1.0`(항상 참)으로 계산되어, 진짜 확인 조건 1개만
   있어도 "2개 확인됨"으로 오판해 조기 손절될 수 있었다. 미준비 시 확인 안 됨(False)으로 수정.
9. **`partial_profit_lock`/`breakout_exhaustion_exit` 수수료 마진 누락 + 국내 매수 중복방지 부재**:
   `time_exit_profit`과 같은 근본원인(수수료 미고려)을 잠재적으로 갖고 있어 동일하게
   `commission_floor` 조건 추가(현재 배포 설정에서는 마진이 넉넉해 미발동, 방어적 조치). 또한
   해외 매수에는 있던 "미체결 매수주문 있으면 재주문 보류/오래되면 취소 후 재주문" 로직이 국내
   매수에는 없어 재시작 타이밍에 따라 중복매수가 재현될 수 있는 경로였다 — 해외와 동일한 로직 이식.

### 검증
- `python3 -m pytest -q` → `528 passed` (신규 회귀 테스트 다수 — 각 수정에 대해 수정 전 코드로
  되돌려 실제로 실패하는지 직접 확인 후 커밋)

## [2026-07-14] `time_exit_profit`이 수수료를 무시하고 매도를 시도하던 근본 원인 수정

### 배경
바로 앞 항목(알림 쿨다운)으로 반복 알림 자체는 막았지만, 사용자가 "주문을 하고 막는 식이 아니라
조건에 부합하지 않는 상황이라면 주문을 그냥 하지 말아야지. 적절한 로직으로 다시 개선할 것"이라고
지적 — 알림 억제는 증상 완화일 뿐이고, 매도 시도 자체가 매 사이클 만들어졌다가 주문 단계에서
막히는 구조 자체를 고치라는 요청.

### 원인
`momentum_policy.evaluate_exit_setup`의 `time_exit_profit` 분기가 `pnl_pct >= 0`(가격 기준 총손익)
만으로 매도를 결정하고 있었다. 같은 함수의 `marginal_profit_exit`은 이미
`commission_floor = commission_rate*2 + 0.003`(기본 약 0.8%) 이상일 때만 매도하도록 만들어져
있었고, `partial_profit_lock`/`breakout_exhaustion_exit`/`take_profit`도 각각 `take_profit_pct`
(1.5%)/`full_take_profit_pct`(2.5%)/`overseas_take_profit_pct`(2.5%) 이상을 요구해 이미 수수료를
넉넉히 넘는 구조였다. `time_exit_profit`만 이 마진 조건이 빠져 있어서, 매입가와 거의 같은 가격에
멈춰있는 포지션도 매 사이클 "매도 시도 → `lab_domestic/overseas_orders.py`의 순손익(수수료 반영)
계산에서 0 이하라 `net_profit_below_cost`로 스킵"이라는 무의미한 왕복을 반복했다.

### 수정
- `src/kinvest_trade/momentum_policy.py`: `time_exit_profit` 분기 조건을 `pnl_pct >= 0`에서
  `pnl_pct >= commission_floor`로 변경. 수수료를 못 넘기는 시간만료 청산은 애초에 매도 후보로
  만들지 않고 계속 HOLD 상태로 남긴다.

### 검증
- `tests/test_momentum_policy.py`: 기존 `test_time_exit_profit_still_works`를 floor를 넘는
  pnl_pct(0.01)로 조정, 신규 `test_time_exit_profit_does_not_fire_below_commission_floor`로
  floor 미달(0.0005) 시 매도 시도 자체가 생기지 않고 hold로 남는지 확인.
- `python3 -m pytest -q` → `514 passed`
- 참고: 다른 익절 사유(`marginal_profit_exit`/`partial_profit_lock`/`breakout_exhaustion_exit`/
  `take_profit`)는 이미 자체 마진 조건이 있어 이번 수정 대상이 아니었음. 앞 항목의 알림 쿨다운은
  이 수정 후에도 다른 스킵 사유(세션 불가, 충돌 주문 등)에 대한 방어선으로 계속 유지.

## [2026-07-14] `net_profit_below_cost` 반복 주문거부 알림 폭탄 수정

### 배경
CRAN 사건으로 의도치 않게 생긴 5,251주 포지션(매입가 $10.20 = 현재가)에 대해 텔레그램으로
`[KIS][거래알림] ... 동작=주문거부 ... 사유=시간 만료 청산(수익) 주문거부=1건 (net_profit_below_cost)`
메시지가 1분 간격으로 계속 반복 발송된다는 신고.

### 원인
`시간 만료 청산(수익)`(비보호성 time exit) 조건은 이 포지션에서 매 사이클 계속 성립하지만,
수수료를 반영한 순손익 추정(`_estimate_overseas_net_pnl`)이 0 이하라 실제 매도 주문은 매번
`net_profit_below_cost`로 스킵된다(`lab_overseas_orders.py`). 이 자체는 수수료보다 작은 이익에
파는 것을 막는 의도된 보호 로직이라 문제가 아니다. 문제는 `liquidity_lab.py`의 `_send_summary`가
이 "무주문 스킵" 상태를 매 사이클(스킵 카운트만 있으면 `action_raw="WAIT"`이어도 조건 없이)
텔레그램으로 그대로 내보내고 있었던 것 — `_display_trade_action`이 `WAIT + skip_count>0`을
`동작=주문거부`로 표시하다 보니 실제 브로커 거부가 아닌 내부 스킵인데도 매번 같은 알림이
무한 반복됐다.

### 수정
- `config/fixed_config.json`/`config.py`: `risk.repeated_skip_notify_cooldown_minutes`(기본 30분)
  추가.
- `liquidity_lab.py`: `_send_summary`에서 `action_raw=="WAIT"`이고 `skip_count>0`인 경우에만
  `_should_send_repeated_skip_notice(market, symbol, skip_top_reasons)`로 동일 (시장, 종목,
  스킵 이유) 조합의 알림을 쿨다운 동안 한 번만 보내도록 억제. 실제 주문이 제출된
  매수/매도/`SELL_REJECTED`(실제 브로커 거부) 알림에는 영향 없음 — 이 가드는 "봇이 아예 주문을
  내지 않은" WAIT 경로에만 적용된다.

### 검증
- `python3 -m pytest -q` → `513 passed` (신규 2건: 동일 스킵이 반복 억제되는지, 쿨다운이 지나면
  다시 보내는지)

## [2026-07-13] 자동 미체결취소가 실제로는 한 번도 매칭되지 않던 두 번째 버그 수정 (주문번호 자릿수 불일치)

### 배경
바로 앞 항목(CRAN 사건 조회 버그 수정) 배포 후, 실제로 자동 미체결취소 스케줄러가 쌓여있던
CRAN 미체결 주문을 정리하는지 라이브로 지켜봤다. 배포 후 15분 넘게 지나도 취소 이벤트가 전혀
발생하지 않았고, 예외 로그(`maintenance_skip`)도 없었다 — 즉 스케줄러는 매번 정상적으로 실행은
되지만 "봇이 제출한 미체결 주문"을 한 건도 못 찾고 조용히 종료되고 있었다.

### 원인
`filter_bot_submitted_domestic_orders`/`filter_bot_submitted_overseas_orders`가 `broker_order_events.
broker_order_no`(주문 접수 응답의 `ODNO`, 10자리 0-패딩. 예: `"0000041501"`)와 실시간 미체결조회
응답의 `odno`(패딩 없음. 예: `"41501"`)를 **그대로 문자열 비교**하고 있었다. 두 값이 같은 주문을
가리켜도 자릿수가 달라 절대 일치하지 않아서, "이 미체결 주문이 봇이 넣은 것인가?" 판정이 항상
거짓으로 나왔다. 결과적으로 자동 미체결취소는 처음부터 이번 배포 전까지 **해외뿐 아니라
국내에서도 실질적으로 한 번도 매칭에 성공한 적이 없었을 것**으로 보인다(국내는 최근 미체결
누적 사례가 없어서 드러나지 않았을 뿐). 기존 테스트들은 두 값을 우연히 같은 형식(둘 다 패딩
있음, 혹은 둘 다 영숫자 조합)으로 맞춰 작성해서 이 불일치를 그대로 통과시키고 있었다.

### 수정
- `src/kinvest_trade/telegram_orders.py`: `_normalize_order_no()` 헬퍼 추가(앞자리 0 제거,
  빈 문자열은 그대로 유지). `filter_bot_submitted_domestic_orders`/
  `filter_bot_submitted_overseas_orders` 양쪽에서 저장된 주문번호와 조회된 주문번호를 비교하기
  전에 항상 정규화하도록 수정.

### 검증
- `python3 -m pytest -q` → `511 passed` (신규 2건: 국내/해외 각각 패딩이 다른 주문번호끼리도
  정상 매칭되는지 직접 검증 — 기존 테스트들의 "우연히 형식이 같음" 함정을 피하려고 의도적으로
  다른 자릿수로 작성)
- 배포 후 계속 라이브로 관찰 예정: 자동 미체결취소가 실제로 CRAN 잔여 미체결을 정리하는지 확인

## [2026-07-13] CRAN 중복매수 사건 근본 수정 + 미체결취소 정책 자동화(수동 메뉴 삭제)

### 배경
사용자가 텔레그램 포트폴리오/거래알림 로그를 붙여넣으며 두 가지를 지시:
1. 미체결 주문 해제를 텔레그램 메뉴(수동 확인/확정)가 아니라 **정책상 자동으로** 처리하도록
   개선하고, 개선 후 해당 텔레그램 메뉴를 삭제할 것.
2. 첨부된 로그의 문제를 확인하고 개선할 것 — 로그에는 `/lab_portfolio`가 해외 CRAN을
   "내부기록=1주 조회결과=없음"으로 불일치 표시한 직후, 1분 뒤 텔레그램 거래알림에 CRAN
   매수접수가 $10.20 x1로 4건 배치 표시되어 있었음.

DB(`broker_order_events`)를 직접 조회해 조사한 결과, 로그에 보인 4건은 훨씨 큰 문제의
일부였음: **CRAN 한 종목에 15:07~15:37 UTC(30분) 동안 60건 이상의 매수주문이 동일가
($10.20)로 반복 제출**되고 있었고, 수량은 643 → 1까지 계속 줄어드는 패턴(가용 매수금액이
주문마다 그만큼 예약/소진되는 것과 일치)이었다. 실시간 KIS 잔고 조회(`overseas-balance-check`)
에서는 CRAN이 전혀 보이지 않아, 이 주문들이 체결된 게 아니라 **취소되지 않은 채 계속 새로
쌓이고 있는 미체결 주문 더미**라는 것을 확인.

### 근본 원인
"이미 이 종목에 대한 미체결 매수주문이 있으면 중복 제출하지 말라"는 로직
(`_find_open_overseas_order` → `get_overseas_order_history`)이 모의투자(`env=vps`) 환경에서
`symbol=""`(종목필터 없음) + `fill_filter="00"`(체결/취소/미체결 전체)로 호출하고 있었다.
KIS 모의투자 주문내역 조회 엔드포인트는 페이지네이션이 동작하지 않고 **최대 15건만
반환**하는데(`CTX_AREA_NK200`/`FK200`을 그대로 되돌려줘도 다음 페이지로 넘어가지 않는 것을
직접 확인), 필터 없이 그날의 전체 이력(체결/취소 포함)을 요청하니 CRAN이 스스로 만든
누적 이력이 그 15건을 다 채워버려서, CRAN의 실제로 아직 살아있는 미체결 주문이 조회
결과에서 완전히 사라지는 상황이 발생했다. 그래서 중복방지 체크는 매 사이클 "미체결 없음"으로
오판했고, 사이즈 결정 로직은 계좌 가용 매수금액이 줄어드는 것만 보고 남은 만큼을 또
새 주문으로 넣기를 반복했다. `symbol=<종목>` + `fill_filter="02"`(미체결만)로 직접 쿼리해보면
모의투자에서도 정상적으로 필터가 걸리는 것을 확인했다 — 애초에 "모의투자는 필터가
안 먹는다"는 가정 자체가 틀렸던 것으로 보인다(아마 다른 이유로 그런 워크어라운드가 들어갔다가
굳어진 것).

### 수정
- `src/kinvest_trade/liquidity_lab.py::_list_open_overseas_orders`: `env != "prod"`일 때
  `symbol=""`, `fill_filter="00"`으로 우회하던 로직 제거. 이제 실계좌/모의투자 구분 없이
  항상 `symbol=<대상종목>`, `fill_filter="02"`로 조회한다. 이 함수가
  `_find_open_overseas_order`/`_find_conflicting_overseas_order`(매수/매도 중복방지, 청산주문
  정체 감지)의 유일한 데이터 소스라서, 이 한 곳을 고치면 관련 로직 전체가 함께 고쳐진다.
- `src/kinvest_trade/telegram_orders.py::load_live_open_overseas_orders`: 모의투자 전용으로
  분기하던 "전체 종목 한 번에 조회" 경로를 제거하고, 실계좌 경로가 쓰던 "최근 거래 종목별로
  나눠서 조회" 방식을 양쪽 환경 공통으로 사용하도록 통일. 이제 이 함수가 쓰이는 `/lab_orders`,
  시작/재개 시 미체결 경고, 자동 미체결취소 스케줄러가 모두 같은(고쳐진) 경로를 탄다.
  더는 쓰이지 않게 된 `parse_live_open_overseas_order_rows`(및 컨트롤러 wrapper)는 제거.

### 미체결 취소 정책 자동화 (수동 메뉴 삭제)
`/lab_cancel_stale_domestic(_confirm)`, `/lab_cancel_stale_overseas(_confirm)` 4개 명령을
전부 삭제했다(디스패치, `parse_command` 매핑, `MENU_CATEGORIES`, `BOT_COMMANDS`, `/lab_status`·
시작/재개 경고에 있던 안내 문구 포함). 대신 이미 스케줄러에 존재하던 자동 취소
(`_maybe_auto_cancel_stale_domestic_orders`/`overseas`, 매 사이클 체크·10분에 한 번 실행·
국내 정규장/해외 주문가능세션에만 동작·30분 이상·**봇이 직접 제출한 주문만** 대상)를 유일한
경로로 남겼다. 이 자동 경로는 원래부터 있었지만 해외 쪽은 위와 같은 조회 버그 때문에 실제로는
거의 작동하지 않고 있었다 — 근본 수정으로 이제야 제대로 동작하게 됐다. 취소 실행 자체를
담당하는 `execute_cancel_stale_domestic_orders`/`execute_cancel_stale_overseas_orders`는
자동 경로가 그대로 재사용하므로 삭제하지 않았고, 삭제한 것은 사용자가 직접 트리거하던
프롬프트/확인 명령뿐이다.

### 검증
- `python3 -m pytest -q` → `509 passed` (신규 회귀 테스트 2건: `_list_open_overseas_orders`가
  실계좌/모의투자 모두에서 항상 `symbol`+`fill_filter="02"`로 호출하는지, `/lab_orders` 등이
  쓰는 `load_live_open_overseas_orders`가 모의투자에서도 종목별 조회로 동작하는지 직접 검증)
- 실계좌 API로 직접 재현: `symbol=""` + `fill_filter="00"`으로는 CRAN 최신 주문이 15건짜리
  결과에서 빠짐 → `symbol="CRAN"` + `fill_filter="02"`로는 정확히 열려있는 주문들만 반환됨을
  확인 후 코드에 반영
- 배포 후 CRAN에 쌓여있던 미체결 주문들을 정리(수동 취소 스크립트로 일괄 처리, 아래 참고)

### 다음 단계
- KIS 쪽 진짜 원인(모의투자 주문내역 API가 왜 15건 고정+페이지네이션 미동작인지)은 우리
  쪽에서 고칠 수 없는 외부 API 특성이라, 이번 수정은 "필터를 제대로 걸어서 15건 제한 안에
  항상 원하는 데이터가 들어오게 한다"는 회피책이다. 같은 15건 제한이 "한 종목에 정말로
  15개 넘는 미체결 주문이 동시에 존재"하는 극단적 상황에서는 여전히 유효하므로, 이번 사건처럼
  중복 제출 버그가 재발하지 않는 한 문제가 되지 않을 것으로 판단.

## [2026-07-13] 미체결 청산주문 정체 방지 + `/lab_reset_all` 전체 초기화 + 텔레그램 메뉴 카테고리화

### 배경
사용자가 두 가지를 물어봄:
1. "pending exit order 주문거부가 발생했었는데, 이에 대한 해결책이 정의되어있는지?" — 실제로
   확인해보니 MSEX 익절(take_profit) 매도 주문이 13:58부터 1시간 가까이 미체결로 남아 있었음.
   원인은 "거부"가 아니라 설계 공백: 보호성 청산(손절/ATR하드스탑 등)은 미체결 45초 경과 시
   자동으로 취소 후 재주문하지만, `take_profit`처럼 보호성이 아닌 청산은 이 로직이 전혀 없어서
   목표가에 안 닿으면 무기한 미체결로 남아 다음 매도 시도를 계속 가로막고 있었음. 게다가 취소
   시도 자체가 실패하는 경우(`pending_exit_cancel_failed`)는 어떤 서킷브레이커에도 연결되어
   있지 않아 조용히 무한 재시도만 반복하는 구조였음.
2. 예전 로그/실적 정리 + 테스트 환경 재구성용 "전체 초기화 후 실계좌 보유만 반영" 기능 요청,
   그리고 "명령이 20개 넘어서 정신없다"는 텔레그램 메뉴 정리/카테고리화 요청.

### 수정
- `src/kinvest_trade/liquidity_lab.py`
  - `_stale_exit_replace_seconds()` 신규: `risk.stale_exit_replace_minutes`(기본 15분)를 반환
- `src/kinvest_trade/lab_domestic_orders.py`, `src/kinvest_trade/lab_overseas_orders.py`
  - 미체결 청산주문 취소 판단을 `exit_reason not in _protective_exit_reasons() or age<45`에서
    `age < (보호성이면 45초, 아니면 stale_exit_replace_minutes)`로 일반화 — 비보호성 청산도
    15분 넘으면 취소 후 현재 호가로 재주문
  - 취소(`revise_or_cancel_*`) 실패 시 `_register_order_rejection(market, side, ...)`을 호출해
    기존 주문거부 서킷브레이커에 편입 — 반복 실패하면 자동 차단 + 텔레그램 알림
- `src/kinvest_trade/config.py`, `config/fixed_config.json`
  - `RiskConfig.stale_exit_replace_minutes: int = 15` 추가
- `src/kinvest_trade/repository.py`
  - `count_rows(table)`, `reset_all_history()` 신규 — `cycle_log`/`event_log`/
    `broker_order_events`/가상거래 3종/`lab_symbol_state` 전량 삭제 (허용 테이블 화이트리스트만)
- `src/kinvest_trade/telegram_control.py`
  - `/lab_reset_all` → 삭제 대상 건수 미리보기 + 확인 요청, `/lab_reset_all_confirm` → DB 백업
    후 `reset_all_history()` 실행 + 연속손절/세션손익/서킷브레이커/쿨다운 등 인메모리 카운터
    초기화. 실계좌 잔고를 별도로 불러오는 단계는 없음 — `lab_symbol_state`를 비우면 다음
    사이클이 평소처럼 KIS 잔고를 실시간 조회해서 캐시를 새로 채우므로 자연히 실계좌 보유만
    반영됨. `telegram_message_log`/`api_call_log`(운영 감사 로그)는 초기화 대상에서 제외
  - `HELP_MESSAGE`를 `MENU_CATEGORIES`(운영 제어/상태 조회/로그 및 성과/주문 정리/데이터 초기화/
    감시종목 설정/테스트) 기반으로 재구성해 카테고리별로 출력
  - `/lab_menu` 신규: 카테고리 인라인 버튼 메뉴. 버튼 클릭(`callback_query`)을 처리해 같은
    메시지를 카테고리별 명령 목록으로 편집(`editMessageText`)하고 `◀ 메뉴`로 되돌아갈 수 있음
- `src/kinvest_trade/notifier.py`
  - `send()`에 `reply_markup` 파라미터 추가, `edit_message()`/`answer_callback_query()` 신규

### 검증
- `python3 -m pytest -q` → `510 passed` (신규 19개: liquidity_lab 6, repository 3, notifier 4,
  telegram_control 6)
- README.md에 미체결 청산주문 정체 방지, `/lab_reset_all` 상세 설명, 카테고리별 명령 목록,
  `/lab_menu` 사용법 반영

### 다음 단계
- `/lab_menu` 버튼은 현재 카테고리 탐색용이며 명령을 직접 실행하지는 않음 — 인자 없는 명령을
  버튼으로 바로 실행하는 것까지 원하면 추가 작업 필요
- `/lab_reset_all`은 사용자가 직접 `/lab_reset_all_confirm`을 입력해야 실행됨(자동 실행 안 함) —
  테스트 환경을 재구성할 때 텔레그램에서 직접 실행

## [2026-07-13] `/lab_gitlog` 5종 로그로 확장 - 요청/응답/텔레그램/API 호출 전체 기록

### 배경
- 사용자 요청: "gitlog에 시스템에서 발생하는 것과 요청 및 그 결과를 모두 포함해서 분석할 수
  있도록 개선" — 요청사항, 텔레그램 알림, 성공/실패/응답, 매매 과정의 요청/응답/후처리까지
  전부 업로드해서 나중에 분석 가능하게 해달라는 요청
- 기존 `/lab_gitlog`는 `cycle_log`(BUY_REAL/SELL_REAL/SKIP만 필터링)와 `event_log` 2종만
  업로드했음. 실제 KIS 주문 요청/응답이 담긴 `broker_order_events`는 전혀 업로드되지 않았고,
  텔레그램 송수신 이력이나 KIS API 호출 자체를 기록하는 테이블도 없었음
- **중요 확인**: 이 저장소(`tagynedlrb/kinvest_trade`)는 **public**. 새 로그를 설계할 때
  계좌번호(CANO)/APPKEY/APPSECRET/HTS ID가 절대 포함되지 않도록 필드를 신중히 골랐음
  (`broker_order_events`의 KIS 응답 원본을 직접 확인해 계좌 정보가 없음을 검증했고,
  API 호출 로그는 요청 바디를 통째로 저장하지 않고 TR_ID/경로/응답코드/메시지 요약만 저장)

### 수정
- `src/kinvest_trade/repository.py`
  - `telegram_message_log`(방향/명령/텍스트/성공여부/오류), `api_call_log`(method/tr_id/
    path/성공여부/http상태/msg_cd/msg1/소요시간) 테이블 신설 + save/list 메서드 추가
- `src/kinvest_trade/notifier.py`
  - `TelegramNotifier`에 선택적 `repository` 파라미터 추가. `send()` 성공/실패 시
    `telegram_message_log`에 자동 기록(레포지토리 없으면 조용히 스킵, 기존 호출부 호환)
- `src/kinvest_trade/telegram_control.py`
  - `_handle_update()`가 인증된 채팅에서 온 모든 명령 텍스트를 `_log_inbound_command()`로 기록
  - `_run_cycle()`의 `KisRestClient`에 `on_api_call=self._log_api_call` 연결
  - `/lab_cb_reset`이 아닌 `/lab_gitlog` 응답 메시지가 `trades`/`events`만 표시하던 것을
    `orders`/`telegram`/`api_calls` 3종도 함께 표시하도록 확장
- `src/kinvest_trade/client.py`
  - `KisRestClient.__init__`에 `on_api_call` 콜백 추가. `_request()`가 매 시도(재시도 포함)마다
    method/tr_id/path/성공여부/http상태/msg_cd/msg1/소요시간을 콜백으로 전달
    (요청 바디·헤더는 전달하지 않음 — CANO/APPKEY/APPSECRET이 여기 있기 때문)
- `src/kinvest_trade/git_uploader.py`
  - `_extract_trade_log`: action_bias 필터 제거 → `cycle_log` 전량 업로드로 확장
  - `_extract_broker_order_log`, `_extract_telegram_log`, `_extract_api_call_log` 신규 추가
  - `upload_log()`를 5종 로그 스펙 테이블(`_LOG_SPECS`) 기반으로 재작성해 파일 종류 추가가
    한 곳만 고치면 되도록 정리

### 검증
- `python3 -m pytest -q` → `491 passed` (신규 15개: repository 2, notifier 3, client 2,
  telegram_control 5, git_uploader 3)
- `client.py` 신규 테스트에서 로그에 계좌번호/appkey/appsecret 문자열이 전혀 포함되지 않는지
  직접 검증(`test_request_reports_api_calls_via_on_api_call_hook`)

### 다음 단계 (의도적으로 이번엔 보류)
- `api_call_log`는 사이클마다 여러 건씩 쌓여 로컬 DB(`data/trading.db`, git에는 안 올라감) 크기가
  빠르게 늘어날 수 있음. 지금은 보류하되, 필요해지면 오래된 행을 주기적으로 정리하는 retention
  정책을 추가하는 게 좋음
- 텔레그램 SENT 로그의 `text` 필드도 원문 그대로 저장 중. 지금 만드는 알림 문구는 계좌 식별
  정보를 담지 않지만, 앞으로 새 알림 문구를 추가할 때는 이 필드가 그대로 public 저장소에
  올라간다는 점을 항상 염두에 둘 것

## [2026-07-13] 실보유 매도 세션차단 시 가상매도 전환 (기존 설계 의도 복원)

### 배경
- 사용자 피드백: "모의투자라 거래가 안 되는 상황이면, 가상거래 시스템의 원래 설계 의도대로
  실계좌였다면 거래 가능한 시점에는 가상매도로 기록하고 이후 정산해야 한다. 지금처럼
  계속 주문거부만 반복하는 건 정상이 아니다"
- 코드 확인 결과 정확히 그 설계가 이미 존재했음:
  - `record_virtual_sell()`: 실보유든 가상보유든 세션 제한으로 실주문이 막히면 가상매도로
    기록(`rejected_error` 파라미터로 "session_not_orderable_in_profile" 사유를 명시적으로
    받는 구조가 이미 있었음)
  - `_reconcile_pending_virtual_sells()`: 모의계좌가 실제 주문 가능해지면 대기 중인 가상매도를
    찾아 실제로 체결하고 정산
  - 즉 매수 쪽(`record_virtual_buy`)은 이미 이 전환을 하고 있었는데, **매도 쪽만 빠져 있었음**
- MSEX(실보유 522주)가 정확히 이 틈에 걸려 있었음: `_place_overseas_sell_order()`가
  세션 제한(`session_not_orderable_in_profile`)을 만나면 두 지점 모두 `record_virtual_sell()`을
  호출하지 않고 단순 skip만 반복 → 실계좌라면 거래 가능한 시간(daytime/premarket/aftermarket)에도
  포지션이 계속 묶여 있었음

### 수정
- `src/kinvest_trade/lab_overseas_orders.py` (`OverseasOrderHelper.place_sell_order`)
  - **사전 체크 경로**: `is_us_orderable_session_for_env(now, env)`가 False여도, 실계좌 기준
    `is_us_orderable_session_for_env(now, "prod")`가 True면 그냥 skip하지 않고
    `record_virtual_sell(..., rejected_error="session_not_orderable_in_profile", ...)` 호출
  - **실주문 시도 후 반응 경로**: 실제 KIS 제출이 세션 제한으로 거부됐을 때
    (`reject_reason == "session_not_orderable_in_profile"`)도 동일하게 실계좌 기준 주문가능
    여부를 확인해 가상매도로 전환
  - 두 경로 모두 실계좌 기준으로도 완전히 거래 불가(주말/공휴일/완전 장마감)면 기존처럼
    그냥 skip 유지 — 가상매도로 전환할 이유가 없는 경우까지 억지로 전환하지 않음

### 검증
- 관련 기존 테스트 3개가 "가상매도로 전환하지 않는다"는 옛 기대값을 그대로 검증하고
  있었음(테스트 이름부터 `_does_not_convert_real_to_virtual_trade`) — 사용자 지침에 따라
  새 기대 동작(가상매도 전환)으로 재작성. 두 테스트가 module-binding 문제로 실제로는
  `liquidity_lab_module`만 패치하고 `lab_overseas_orders_module`은 그대로 둬서 실제 코드 경로에
  영향을 못 주고 있던 것도 함께 고침(이 세션에서 반복 발견된 패턴)
- 반응 경로(실주문 시도 후 KIS가 세션 사유로 거부하는 경우) 전용 테스트 신규 추가
- 완전 장마감(실계좌도 거래불가) 시나리오에서는 여전히 skip만 하는 것을 확인하는 테스트 추가
- `python3 -m pytest -q` → `476 passed`

### 다른 종목 감시/거래 현황 조사 (사용자 질문 2)
- 오늘(7/13) `broker_order_events` 전체: 국내매수거부 91건(오전에 조치), 해외가상매수 2건
  (INVA, QFIN), 국내매도체결 1건 — 이게 오늘 전부였음
- 국내: 06:32 UTC(15:32 KST) 장마감 이후로는 추가 거래 자체가 불가능한 시간대였고, 그 전
  시간은 전량 국내매수거부 사고로 막혀 있었어서 다른 체결이 거의 없었던 것으로 확인
- 해외: 확인 시점 아직 프리마켓이라 모의계좌 실주문 자체가 원천적으로 막혀 있는 시간대.
  INVA/QFIN은 오늘 가상매수로 신규 진입했고 정상적으로 추적되고 있음
- 참고 관찰(추가 조치는 보류): INVA/QFIN의 `lab_symbol_state`가 현재 `stale_signal_cache`로
  표시됨(차트 신호를 가져오지 못해 이전 캐시로 대체 중) — 보유 종목은 순위와 무관하게 항상
  신호 조회 대상에 포함되도록 이미 구현돼 있어 로직상 문제는 아니지만, 데이터 공급단에서
  일시적으로 못 가져오는 것으로 보임. 다음 사이클에도 계속되면 별도로 조사 필요

## [2026-07-13] MSEX 매도거부 조사 - `/lab_portfolio` 보유상태 불일치 감지 추가

### 배경
- 사용자 보고: 매도거부가 발생하면서 "현재 계정에서 거래 불가능"으로 표시되는데, 같은 시점
  `/lab_portfolio`에는 해당 종목을 보유하지 않은 것으로 나온다는 불일치 제보
- 실제 KIS 잔고를 직접 조회(`overseas-balance-check`)해서 확인한 결과:
  - MSEX 522주 실보유 확인, `ord_psbl_qty=522`(전량 주문가능), +2.52% 수익 상태
  - `lab_symbol_state`도 동일한 522주/평단가/현재가를 정확히 추적 중이었음
  - 즉 "거래 불가능" 자체는 데이터 오류가 아니라 KIS 모의투자 API가 프리마켓/데이타임 등
    정규장 외 시간에는 미국주식 주문 제출 자체를 막는 정책 때문이었음
    (`is_us_orderable_session_for_env(env="vps")`는 `session=="regular"`일 때만 허용,
    확인 시점은 `premarket`이라 정규장 시작(약 1시간 20분 후)까지 대기가 정상 동작)
  - `is_us_regular_session()`은 이름과 달리 "시장이 완전히 닫힌 게 아니면 True"를 반환하는
    함수라 `us_open` 판정과 `is_us_orderable_session_for_env`의 엄격한 `regular` 판정이
    서로 다른 기준을 쓰는 것처럼 보였을 뿐, 실제로는 의도된 이중 기준(감시는 넓게, 모의투자
    주문 제출은 정규장에만 좁게)이었음 — 여기서는 버그를 찾지 못함
- 다만 `/lab_portfolio`의 "실보유 없음" 표시는 별개의 진짜 문제로 확인됨:
  - `/lab_portfolio`는 메인 루프와 별개의 임시 `KisRestClient`로 실시간 잔고를 재조회하는데
    (`_load_live_portfolio_positions`), 이 재조회가 부분적으로 실패하거나 예외가 나면
    `_logger.warning`으로만 남고 화면에는 아무 표시 없이 조용히 빈 목록으로 대체됨
  - 이 상태에서 `real_positions_override`가 빈 리스트(`[]`, `None`이 아님)로 전달되면
    `build_portfolio_message`가 그걸 "진짜로 보유 없음"으로 그대로 표시 — 매매 루프 자신의
    캐시(`lab_symbol_state`)는 정확히 522주를 추적 중인데도 화면은 다르게 보이는 정확히
    이번 불일치 상황이 재현됨

### 수정
- `src/kinvest_trade/telegram_reports.py`
  - `ReportHelper.detect_holding_mismatch_lines()` 추가: 화면에 표시되는 실보유 목록과
    `lab_symbol_state.has_position=1`(가상보유 종목은 제외) 캐시를 대조해서, 루프는
    보유중으로 기록했는데 화면에는 없는 종목을 찾아냄
  - `build_portfolio_message()`에 `─── 보유상태 불일치 ───` 섹션 추가
    (`내부기록=N주 조회결과=없음 조치=재조회 또는 /lab_orders 확인`)
- `src/kinvest_trade/telegram_control.py`
  - `_detect_holding_mismatch_lines()` 얇은 wrapper 추가
- `README.md`
  - `/lab_portfolio` 불일치 감지 동작 설명 추가

### 검증
- 실제 운영 DB로 직접 재현: `real_positions=[]`(재조회 실패 시나리오)를 넣으면 MSEX가
  정확히 `해외 MSEX 내부기록=522주 조회결과=없음`으로 검출됨을 확인
- `python3 -m pytest -q` → `475 passed` (신규 3개: 불일치 검출/정상표시/가상보유 제외)

### 다음 단계
- 이번엔 감지(경고 표시)까지만 구현. 재발 시 자동으로 라이브 재조회를 한 번 더 시도하는
  자동 복구까지 붙이는 건 다음 단계로 미룸

## [2026-07-13] 주문거부 서킷브레이커 추가 + 국내 매수 100% 거부 사고 대응

### 배경
- 사용자 요청("gitlog 보고 개선 진행, 특히 주문 거부 건에 대한 처리")에 따라 실제 운영 DB의
  `broker_order_events`를 조사한 결과, 오늘(7/13) 국내 정규장 개장(00:14 UTC)부터 장마감
  (06:32 UTC)까지 **국내 매수 주문 91건이 전량 거부**됐음을 확인
  - 전량 동일 오류: `VTTC0012U http_error=500 IGW00007 MCA 전문바디 구성 중 오류가 발생하였습니다`
  - 10개 이상 다른 종목(360750, 005930, 379800, 069500 등)에 걸쳐 발생 → 특정 종목/가격/
    수량 문제가 아니라 국내 매수 요청 자체가 KIS 게이트웨이에서 구조적으로 거부되는 상황
  - 같은 시간대 국내 매도는 1건 정상 접수됨 → 계좌/토큰 문제가 아니라 매수 경로 한정 이슈로 추정
  - `place_cash_order()`(client.py) 자체는 최근 변경 이력이 없어 코드 회귀는 아님. KIS
    모의투자 측 사유일 가능성이 높으나 외부 문서 확인이 불가해 근본원인은 미확정
- 더 근본적인 문제: 매도 주문거부는 종목별 쿨다운(국내 10분/해외 20분)이 있었지만, **매수
  주문거부에는 백오프가 전혀 없어** 같은 오류가 나는 6시간 넘게 사이클마다 계속 재시도함.
  근본원인을 못 고치더라도, 이렇게 반복되는 실패를 조용히 방치하지 않는 장치가 필요했음

### 수정
- `src/kinvest_trade/lab_risk.py` (`CircuitBreakerManager`)
  - `record_order_rejection()`, `is_order_reject_halted()`, `order_reject_status()`,
    `reset_order_rejections()` 추가
  - 시장×방향(`domestic:buy` 등) 기준으로 최근 N분 내 거부 횟수를 추적, 임계치 초과 시
    해당 시장/방향만 쿨다운 동안 차단. 연속손절 CB와 별개 상태로 관리
- `src/kinvest_trade/config.py`, `config/fixed_config.json`
  - `risk.order_reject_threshold=5`, `order_reject_window_minutes=15`,
    `order_reject_cooldown_minutes=30` 추가
- `src/kinvest_trade/liquidity_lab.py`
  - `_register_order_rejection()`, `_is_order_reject_halted()` 추가 (실제 KIS 오류
    메시지를 담아 발동 시 텔레그램 알림)
  - `_run_cycle()`의 국내 매수 예산/해외 진입 슬롯 계산에 차단 상태 반영,
    `domestic_order_reject_halted`/`overseas_order_reject_halted` skip 사유 기록
- `src/kinvest_trade/lab_domestic_orders.py`, `lab_overseas_orders.py`
  - 매수/매도 주문거부(`KisApiError`) 4개 지점 모두에서 `_register_order_rejection()` 호출
    (기존 매도 쿨다운은 유지, 시스템 전반 거부 폭주 감지는 신규 추가)
- `src/kinvest_trade/telegram_control.py`
  - `/lab_cb_reset`이 주문거부 서킷브레이커도 함께 초기화하도록 확장
- `src/kinvest_trade/telegram_reports.py`
  - `/lab_guard`에 `주문거부차단=시장:방향(N회) 확인=/lab_cb_reset` 줄 추가

### 부수 발견 - 기존 버그 수정
- 새 기능 테스트 도중 `CircuitBreakerManager._emit_event()`가 `event_hook`을 위치 인자로
  호출(`self._event_hook(event_type, detail)`)하는데, 실제 프로덕션에 연결된
  `event_hook=self._save_event`는 keyword-only 시그니처라 **매번 `TypeError`가 발생해
  `try/except`로 조용히 삼켜지고 있었음**. 즉 기존 연속손절/일일손실 서킷브레이커가 발동/해제될
  때마다 `event_log`에 `cb_fired`/`cb_released` 이벤트가 저장된 적이 한 번도 없었음(로그에만
  남고 DB에는 기록되지 않음)
- `_emit_event()`가 keyword 인자로 호출하도록 수정(`event_hook(event_type=..., detail=...)`).
  기존 테스트 더블(위치 인자 이름 그대로인 람다)과도 호환되어 회귀 없음
- 회귀 테스트 추가: 실제 `_save_event`를 event_hook으로 연결한 상태로 CB를 발동시켜
  `event_log`에 실제로 저장되는지 확인(`test_register_order_rejection_saves_event_via_real_save_event_hook`)

### 미해결
- 오늘 91건 거부의 KIS 측 근본원인(IGW00007)은 이번 조치로 해소되지 않는다. 이 서킷브레이커는
  "같은 오류로 무한 재시도하며 조용히 소진되는 것"을 막고 사용자에게 즉시 알리는 안전장치이며,
  내일 국내장 재개 시 같은 오류가 재발하면 5회 이내에 자동 차단 후 텔레그램으로 알린다.
  KIS 모의투자 계좌 상태/공지사항 확인이 별도로 필요할 수 있음

### 테스트
- `python3 -m pytest -q` → `472 passed` (신규 12개: lab_risk 6, liquidity_lab 4, telegram_control 2)

## [2026-07-11] telegram_control 분리 2차 - `telegram_reports.py` (상태/포트폴리오/성과 리포트 위임)

### 배경
- 1차(`telegram_orders.py`) 분리 후 `telegram_control.py`에 남은 최대 블록은
  상태/워치리스트/포트폴리오/성과/가드 메시지 생성 영역(약 1,500줄)이었음
- 최근 이틀간 반복 수정이 가장 많이 몰렸던 영역이라, 분리해 두면 이후 메시지 수정
  diff가 리포트 전용 모듈 안에 갇혀 리뷰가 쉬워짐

### 수정
- `src/kinvest_trade/telegram_reports.py` 신설 (1,599줄)
  - `ReportHelper` 추가 — `OrderAdminHelper`와 동일한 helper 패턴
  - 이동 메서드 38개: `/lab_status`·`/lab_watchlist`·`/lab_portfolio`·`/lab_log`·
    `/lab_performance`·`/lab_report`·`/lab_guard` 메시지 생성, 실시간 가격/주문가능
    USD/포지션 조회, 가상 노출·정리후보·리스크 라인 생성
- `src/kinvest_trade/telegram_control.py`
  - 3,781줄 → 2,467줄. 이동 메서드는 원 시그니처 그대로 얇은 wrapper로 전환
  - `self.reports` 초기화 + `_get_report_helper()` lazy 접근자 추가
- monkeypatch 대응
  - 테스트가 `telegram_control` 모듈 레벨에서 패치하는 7개 이름(기존 5개 +
    `get_us_trading_session`, `is_us_orderable_session_for_env`)은 이동한 본문에서
    `from . import telegram_control as _tc` 지연 임포트로 참조
- 이번 분리로 `telegram_control.py`는 수명주기(run/스케줄러/명령 루프), 명령 핸들러,
  세션 성과 누적, 공용 포매터만 남음. 파일 구성: control 2,467 / reports 1,599 / orders 978

### 검증
- `python3 -m pytest -q` → `460 passed`
- 양방향 임포트 순서 sanity check 통과

## [2026-07-11] 서비스 재시작 시 running 모드 보존 (SIGTERM 강제 stopped 제거)

### 배경
- 배포 루틴에서 `systemctl --user restart` 직후 거래 루프가 항상 `stopped`로 돌아가는
  원인을 확인: `run()`의 SIGTERM 처리 경로가 `mode`를 무조건 `"stopped"`로 덮어쓰고
  저장하고 있었음. 그동안 "루프가 자꾸 중지 상태로 발견되던" 현상의 주요 원인
- 사용자 지침 6번("개선 업데이트 후 기본적으로 lab start 수행")을 구조적으로 구현:
  서비스 재시작은 사용자의 정지 명령이 아니므로 이전 모드를 그대로 보존해야 함

### 수정
- `telegram_control.py` `run()`
  - SIGTERM 수신 시 `mode`를 강제 변경하지 않고 현재 모드 그대로 저장
  - running 상태에서 재시작하면 기동 후 루프 자동 재개, 사용자가 `/lab_stop`으로
    정지시킨 상태라면 재시작 후에도 stopped 유지 (의도된 정지는 존중)
- `tests/test_telegram_control.py`
  - SIGTERM 테스트의 단언을 새 의미(running 보존)로 갱신

### 검증
- `python3 -m pytest -q` → `460 passed`

## [2026-07-11] telegram_control 분리 1차 - `telegram_orders.py` (주문 취소/감사 위임)

### 배경
- 전체 점검에서 `telegram_control.py`(4,542줄)가 `liquidity_lab.py`보다 커져 다음 분리
  대상으로 지목됨. 사용자 피드백 7번(후속 개선 계속)에 따라 첫 증분을 진행
- 분리 단위는 점검 때 식별해 둔 자연 경계 중 자립성이 가장 높은 "미체결 취소 + 실시간
  미체결 조회 + 주문 감사 포맷" 영역(약 920줄)

### 수정
- `src/kinvest_trade/telegram_orders.py` 신설 (978줄)
  - `OrderAdminHelper` 추가 — `lab_overseas_orders.py`와 동일한 helper 패턴
    (controller 역참조, 본문 `self.` → `controller.` 치환)
  - 이동 메서드 23개: 국내/해외 미체결 취소 프롬프트·실행·자동취소, bot 제출 주문
    필터, `/lab_orders` 메시지 생성, 실시간 미체결 조회/파싱/포맷, 주문 감사 라인 포맷
- `src/kinvest_trade/telegram_control.py`
  - 4,542줄 → 3,779줄. 이동 메서드는 원 시그니처 그대로 얇은 wrapper로 전환
  - `self.order_admin` 초기화 + `_get_order_admin_helper()` lazy 접근자 추가
    (`__new__` 기반 테스트 인스턴스 보호, `liquidity_lab.py` 관용구 복사)
- 순환 임포트/monkeypatch 대응
  - 테스트가 `telegram_control` 모듈 레벨에서 패치하는 5개 이름(`KisRestClient`,
    `LiquidityLabService`, `is_krx_holiday`, `is_krx_regular_session`, `is_nyse_holiday`)은
    이동한 본문에서 메서드 내부 `from . import telegram_control as _tc` 지연 임포트로 참조
    (모듈 상단 임포트는 순환이라 불가, 기존에 확인된 monkeypatch 함정 회피)

### 검증
- `python3 -m pytest -q` → `460 passed`
- 양방향 임포트 순서 sanity check 통과

## [2026-07-11] 사용자 피드백 7건 반영 - 손절 확인 로직, 통합 포지션 한도, 전략 근거 검증

### 배경
- 텔레그램 변경점 요약에 대한 사용자 피드백 7건을 받아, 외부 레퍼런스 리서치(웹 검색
  기반 다중 소스 교차검증)와 코드 조사(병렬 3건)를 먼저 수행한 뒤 필요한 것만 수정함

### 항목별 결론
1. **단독신호 차단 → 중복신호 요구가 올바른가?** (리서치 결과: 유지)
   - 주류 트레이딩 교육/전략 소스들이 일관되게 "단독 지표보다 다중 지표 확인"을 권장
     (VWAP+RSI/VWAP+거래량 조합 권장, VWAP 이탈은 거래량 동반 확인 요구, MACD는 ADX
     동의 필요 등 — 검증 표결 3-0 다수). 짧은 타임프레임(1분봉)일수록 허위신호가 많아
     확인 요구가 더 중요하다는 소스도 확인.
   - 트레이드오프(신호 수 감소 vs 허위신호 감소)는 인지된 상태로, 손실이 확인된 해외
     한정 차단이므로 현행 유지. 전략별 성과 추적(entry_by 라벨)은 계속 기록되므로
     "전략 묶음 관리" 취지도 유지됨. 코드 변경 없음.
2. **후보는 이미 고유동성인데 왜 별도 거래량 보류가 있나?** (조사 결과: 중복 아님, 유지)
   - 후보 필터는 "종목 전반의 절대 유동성"(가격 하한, 일 거래대금, 스프레드) 검사.
   - `overseas_min_strategy_volume_ratio`(0.8)는 "지금 이 순간 그 종목 자신의 평소
     분봉 거래량 대비 비율" 검사 — 평소엔 유동성이 충분한 종목도 특정 분봉에서 거래가
     말라붙는 순간(개장 직후/점심 소강 등)이 있고, 그 순간의 진입만 보류하는 것.
   - 서로 다른 축이므로 중복이 아님. 코드 변경 없음. 텔레그램 브리핑으로 설명.
3. **일시적 급락(단일 체결 wick)에 손절하지 않기** (리서치+구현)
   - 기존 해외 고정 손절은 순수 가격(pnl_pct)만 보고 마지막 체결가 1건으로도 발동
     가능했음. 거래량은 어떤 손절 트리거에도 사용되지 않았음(조사로 확인).
   - 리서치 컨센서스: 거래량 동반 확인(가격 신호는 거래량이 같은 방향일 때 신뢰),
     멀티바/시간 확인, ATR 기반 동적 스탑, 단 깊은 손실은 즉시 손절(하드 플로어).
   - 구현: `_overseas_stop_loss_confirm_reason()` 신설 (`liquidity_lab.py`)
     - 손절 기준을 갓 넘긴 첫 관측 + 거래량 미확인 → 한 사이클 대기(`stop_loss_confirm_wait`)
     - 다음 사이클에도 손절권 → 지속성 확인으로 간주하고 손절 진행
     - 분봉 거래량 1.5배 이상 급증 + 음봉(`overseas_stop_loss_volume_confirm_ratio`) → 즉시 손절
     - 손실이 기준의 2.0배 초과(`overseas_stop_loss_hard_multiplier`) → 즉시 손절 (하드 플로어)
     - 손절권 위로 회복 시 대기 기록 초기화, 대기 기록 10분 초과 시 재관측으로 취급
   - ATR/모멘텀 계열 청산(`atr_hard_stop`, `momentum_loss_cut` 등)은 이미 2-of-3
     다중조건 확인이 있어 손대지 않음.
4. **국내/해외 한도 분리 이유?** (조사 결과: 기술적 사유 없음 → 합산 한도 신설)
   - KIS 클라이언트는 국내/해외가 단일 세션·단일 토큰·공용 백오프를 쓰므로 트래픽
     제약으로 분리할 이유 없음. WORKLOG 이력상 분리 한도는 순수 성과 기반 임의
     튜닝이었음(해외 20→8 손실 때문, 국내 5→8 성과 좋아서).
   - `max_concurrent_total_positions`(기본 10) 신설: 국내+해외 합산 보유 종목 수 상한.
     기본값 10은 `slot_entry_pct=0.10` 기준 자본 100% 배치에 해당하는 구조적 근거값
     (성과 튜닝 아님). 시장별 한도(8/8)는 유지하되, 사용자 지침대로 당분간 성과 기반
     한도 조정은 하지 않음을 README에 명시.
5. **20% 급등락 가드** (3번과 계층 분리로 정리)
   - 기존 20% 쇼크 가드는 데이터 오류(이상 호가) 차단용으로 유지. 새 손절 확인(3번)이
     현실적인 1~2% 급락 구간의 회복 가능성 판단을 담당. 두 계층의 역할 구분을 README에 명시.
6. **개선 업데이트 후 lab start 기본 수행** — 운영 루틴에 반영(이번 배포부터 적용).
7. **후속 개선 계속** — 다음 단계로 `telegram_control.py` 분리 작업 진행 예정.

### 수정
- `src/kinvest_trade/config.py`, `config/fixed_config.json`
  - `max_concurrent_total_positions=10`, `overseas_stop_loss_confirm_enabled=true`,
    `overseas_stop_loss_hard_multiplier=2.0`, `overseas_stop_loss_volume_confirm_ratio=1.5`,
    `overseas_stop_loss_confirm_max_age_sec=600` 추가
- `src/kinvest_trade/liquidity_lab.py`
  - `_overseas_stop_loss_confirm_reason()`, `_clear_overseas_stop_loss_confirm()` 추가
  - `_run_cycle()` 매수 예산 계산에 합산 한도 반영, 합산 한도로 막히면
    `total_position_cap_reached` 사유 기록
- `src/kinvest_trade/lab_watch.py`
  - 고정 손절 분기에서 확인 가드 호출, 손절권 회복 시 가드 해제
  - `remaining_total_position_slots()` static helper 추가
- `tests`
  - 손절 확인 4종(한 사이클 대기 후 확인, 거래량 확인 즉시, 깊은 손실 즉시, 회복 시
    가드 해제), 합산 한도 2종(슬롯 계산, `_run_cycle` skip 사유), config 로드 검증 추가
  - 기존 우선순위 테스트는 `overseas_stop_loss_confirm_enabled=False`로 목적 유지

### 검증
- `python3 -m pytest -q` → `460 passed`
- 기존 `test_overseas_exit_price_shock_requires_confirmation`(-35% 딥로스)은 하드
  플로어 경로로 수정 없이 통과 — 깊은 손실의 즉시 손절 동작 보존 확인

## [2026-07-11] 전체 코드 점검 - 죽은 코드 제거, 중복 통합, README 정비

### 배경
- 10차 리팩터링 이후 사용자 요청으로 전체 코드/구성을 다시 점검. 최근 이틀간(7/10~7/11)
  약 100건의 빠른 반복 작업이 있었던 만큼, "잘못 개선했거나 불필요해진 것"이 남아있는지
  4개 영역(telegram_control.py, liquidity_lab.py+helper 모듈, 레거시 엔트리포인트,
  최근 이틀 변경 요약)을 병렬로 감사한 뒤 결과를 종합해 정리함
- 감사 결과 `auto_trader.py`(대체 모드, 테스트 통과 중), `git_uploader.py`(`/lab_gitlog`로
  실사용 중)는 정상 기능으로 확인되어 유지. `watcher.py`/`run_watch.py`는 최근 활동은
  없지만 README에 문서화된 기능이라 이번엔 손대지 않음

### 수정 - 죽은 코드 제거
- `src/kinvest_trade/liquidity_lab.py`
  - 미사용 import 5개 제거: `format_krw`, `VirtualPosition`, `apply_override`,
    `compute_adaptive_override`, `detect_market_regime` (각각 실제 사용처인
    `lab_domestic_orders.py`/`lab_watch.py`/`lab_positions.py`/`auto_trader.py`에는
    이미 직접 import되어 있어 영향 없음)
  - 호출부가 전혀 없는 orphan 메서드 9개 제거: `_remember_persisted_symbol_state`,
    `_snapshot_from_payload`(참고: `@staticmethod`인데 본문에서 `self`를 참조하는
    잠재 버그가 있었음 - 죽은 코드라 실행된 적 없이 발견됨), `_with_live_price`,
    `_state_snapshot_with_live_price`, `_persist_watch_target_state`,
    `_restore_strategy_position`, `_no_orderable_retry_minutes`,
    `_build_domestic_watch_targets`(국내 전용 watch target 생성 - `_build_unified_watch_targets`로
    완전히 대체되어 호출이 끊긴 구버전), `_commission_rate`(`_domestic_commission_rate`/
    `_overseas_commission_rate` 분리 이후 남은 잔재)
  - `tests/test_liquidity_lab.py`: `VirtualPosition` import를 `lab_positions`에서 직접 하도록 수정
- `src/kinvest_trade/telegram_control.py`
  - 어떤 `/lab_*` 명령에서도 도달 불가능한 `_send_positions_message`,
    `_build_virtual_portfolio_message`, `_send_virtual_portfolio_message` 제거
    (구버전 `[KIS][VIRTUAL_PORTFOLIO]` 메시지 포맷 - 현재의 `_build_portfolio_message`로
    완전히 대체됨)
  - `tests/test_telegram_control.py`: 위 메서드 전용 테스트 1개 제거
- `examples/sample_snapshots.json` 제거 (코드/README 어디서도 참조하지 않는 초기 개발기 fixture)

### 수정 - 중복 로직 통합
- `src/kinvest_trade/telegram_control.py`
  - `max_concurrent_overseas_orders` 설정값을 읽는 동일한 3줄이 5곳
    (`/lab_reset`, `/lab_trim_virtual`, `/lab_status`, `/lab_portfolio` 청산후보,
    `/lab_portfolio` 노출요약)에 각각 반복돼 있어 `_max_concurrent_overseas_positions()`
    헬퍼로 통합
  - 가상보유를 `(시장, 통화)` 기준으로 묶어 종목수/노출금액을 계산하는 동일한 루프가
    3곳(`/lab_reset`, `/lab_status`, `/lab_portfolio` 노출요약)에 반복돼 있어
    `_group_virtual_positions_by_market_currency()` 헬퍼로 통합
  - `_format_saved_price_age(분단위)`와 `_format_recent_age_text(datetime)`가 동일한
    분/시간/일 표시 규칙을 각각 구현하고 있어, 후자가 전자를 호출하도록 통합
  - 위 통합은 모두 출력 문구를 그대로 유지하는 순수 리팩터링이라 기존 메시지 포맷
    테스트가 그대로 통과함

### 결과
- `liquidity_lab.py`: 4,872줄 → 4,737줄
- `telegram_control.py`: 4,630줄 → 4,542줄
- README.md: `lab_*.py` 분리 모듈 구조 반영, `/lab_trim_virtual`·`/lab_reset`·`/lab_relist`·
  `/lab_cb_reset`·`/lab_gitlog` 등 그동안 문서화되지 않았던 명령 6개 추가, 해외
  `VOL` 단독 진입 차단(`overseas_block_standalone_vol`) 반영

### 다음 단계 (의도적으로 이번엔 보류)
- `telegram_control.py`(4,542줄)가 이제 `liquidity_lab.py`보다도 커서, `liquidity_lab.py`를
  10차례에 걸쳐 분리했던 것과 같은 방식으로 `telegram_reports.py`(상태/포트폴리오/성과
  메시지 생성, 약 2,000줄), `telegram_orders.py`(미체결 취소/주문감사, 약 950줄) 분리가
  다음 후보. 이번 세션에서는 하지 않음 - 자금이 실제로 운용 중인 상태에서 대형 구조
  변경을 다른 정리 작업과 한 번에 묶는 것은 리스크가 커서, `liquidity_lab.py`와 동일하게
  여러 세션에 걸쳐 점진적으로 진행하는 편이 안전하다고 판단
- `self._exit_cooldown` 등 helper 분리 과정에서 남겨둔 레거시 동기화 상태(`_sync_runtime_legacy_state`)는
  더 이상 외부에서 직접 읽지 않는다면 수집 가능하나, 이번엔 범위에서 제외
- `watcher.py`/`run_watch.py`는 최근 실사용 흔적이 없지만 README 문서화 기능이라 유지 여부는
  사용자 확인 후 결정

### 테스트
- `python3 -m pytest -q`
- 결과: `454 passed` (죽은 코드 전용 테스트 1개 제거로 455 → 454, 그 외 회귀 없음)

## [2026-07-11] 지시문 #68 10차 반영 - 해외 청산대상 선정 위임 (`lab_watch.py`)

### 배경
- 9차 반영 이후 `liquidity_lab.py`에 남은 가장 큰 블록은 `_select_overseas_exit_targets()`
  (약 250줄)였음
- `lab_watch.py`(`WatchStateHelper`)에는 이미 `select_domestic_exit_target()`,
  `select_overseas_buy_targets()`, `select_domestic_buy_targets()`가 있어
  "해외 청산대상 선정"만 짝이 맞지 않는 상태였음 — watch-target 선정 계열 4종 중 유일하게
  `liquidity_lab.py`에 남아있던 로직이라 이관 대상이 명확했음
- 이 메서드는 실주문 제출이 아니라 순수 선정/우선순위 로직(실보유+가상보유 병합, 손절/익절
  우선순위, no-orderable 재시도 상태 갱신)이라 8~9차의 주문 helper들과는 다른 성격이지만,
  기존 `select_overseas_buy_targets()`와 동일하게 `service = self.service` 위임 패턴으로
  옮기기에 적합했음

### 수정
- `src/kinvest_trade/lab_watch.py`
  - `WatchStateHelper.select_overseas_exit_targets()` 추가
  - 실보유/가상보유 병합 스캔, no-orderable 재시도 상태 갱신, 손절/익절 우선순위 정렬 이관
  - `OverseasHeldPosition` 생성자 호출이 필요해 순환 임포트를 피하기 위해 메서드 내부에서
    `from .liquidity_lab import OverseasHeldPosition`로 지연 임포트
- `src/kinvest_trade/liquidity_lab.py`
  - `_select_overseas_exit_targets()`를 얇은 helper wrapper로 전환
  - 짝인 `_select_overseas_exit_target()`(단수, max_exits=1 래퍼)은 그대로 유지

### 결과
- `liquidity_lab.py`: 5,107줄 → 4,872줄
- `lab_watch.py`: 1,010줄 → 1,261줄
- watch-target 선정(국내/해외 매수, 국내/해외 청산) 4종 로직이 모두 `lab_watch.py`로 통일됨

### 테스트
- `python3 -m pytest tests/test_liquidity_lab.py -q`
- `python3 -m pytest tests -q`
- 결과
  - 이번 이관은 module-level monkeypatch 의존이 없어 1차 실행부터 `455 passed`

### 다음 단계
- `liquidity_lab.py`에 남은 최대 블록은 `_run_cycle()`(약 360줄, 사이클 오케스트레이션
  본체)과 `scan_overseas()`(약 130줄). `_run_cycle()`은 여러 helper 호출을 순서대로
  엮는 최상위 진입점 성격이 강해, 다음 단계에서는 "그대로 이관"보다 하위 단계(스캔 결과
  캐싱, 세션 판정)를 먼저 별도 helper로 뽑아내는 편이 안전할 것

## [2026-07-11] Claude Code 인수 점검 + 지시문 #68 9차 반영 - 해외 매도 제출 위임

### 배경
- codex 세션에서 이어받아 상태를 먼저 점검함. 다음 두 가지 운영 공백을 확인:
  - 8차 분리(`lab_overseas_orders.py` 확장)까지의 로컬 커밋 4개(`4dc877b`~`2814fac`)가
    원격(`origin/master`)에 아직 push되지 않은 상태였음
  - `kinvest-telegram-control.service`가 00:02 KST 직전 `/lab_stop` 계열 조작 이후
    재기동되지 않아 `inactive(dead)` 상태로 약 4~5시간 방치됨 (토요일 새벽이라 국내/미국
    정규장은 모두 휴장이라 실거래 공백 자체의 실질 영향은 없었음)
- 8차 반영 시점에 남겨둔 "다음 단계" 메모대로, `liquidity_lab.py`에 남아있던 마지막 대형
  해외 주문 메서드 `_place_overseas_sell_order()`(약 740줄)를 `OverseasOrderHelper`로
  이관해 해외 매수/매도 제출 경로를 모두 helper 쪽으로 통일함

### 수정
- `src/kinvest_trade/lab_overseas_orders.py`
  - `OverseasOrderHelper.place_sell_order()` 추가
  - 해외 실매도 제출, 미체결 매수/매도 정리, net PnL 컷오프, circuit breaker 알림,
    체결 로그 저장까지 이관
  - `asyncio`, `logging`, `is_us_orderable_session_for_env` import 추가
- `src/kinvest_trade/liquidity_lab.py`
  - `_place_overseas_sell_order()`를 얇은 helper wrapper로 전환
- `tests/test_liquidity_lab.py`
  - `_force_overseas_orderable_session()`이 `liquidity_lab_module`뿐 아니라
    `lab_overseas_orders_module`의 `is_us_orderable_session_for_env`도 함께 패치하도록 수정
    (이관 후 실제 호출 지점이 새 모듈로 옮겨가 기존 monkeypatch가 무력화되던 문제 수정)

### 결과
- `liquidity_lab.py`: 5,835줄 → 5,107줄
- `lab_overseas_orders.py`: 1,018줄 → 1,765줄
- 해외 주문 영역의 실매수/실매도 제출 로직이 모두 `OverseasOrderHelper`로 모여,
  `liquidity_lab.py`에는 라우팅/오케스트레이션 성격 코드만 남음

### 테스트
- `python3 -m pytest tests/test_liquidity_lab.py -q`
- `python3 -m pytest tests -q`
- 결과
  - 이관 직후 1차 실행: 해외 매도 관련 10개 실패 (모두 `is_us_orderable_session_for_env`
    monkeypatch가 옛 모듈 경로만 패치해 발생)
  - 테스트 fixture 수정 후 재실행: `455 passed`

### 다음 단계
- `liquidity_lab.py`에 남은 remaining 대형 블록은 watch target 선정/오케스트레이션
  성격이 강해, 이후 단계에서는 순수 로직 분리보다 orchestration 흐름 정리가 우선일 것
- 이번 세션에서 확인한 운영 루틴(작업 후 WORKLOG 기록 → git push → 서비스 재기동 →
  텔레그램 알림)을 이어서 수행함

### 배경
- 7차 분리 이후에도 해외 실주문 진입 메서드 `_place_overseas_test_order()`는 여전히
  `liquidity_lab.py` 안에 남아 있었음
- 이 메서드는 가상매수 fallback, 해외 미체결 정리, 체결 로그 저장까지 함께 품고 있어
  이미 분리된 `OverseasOrderHelper` 쪽으로 이동시키면 해외 주문 계층의 책임이 더 자연스럽게 정리됨

### 수정
- `src/kinvest_trade/lab_overseas_orders.py`
  - `OverseasOrderHelper.place_test_order()` 추가
  - 해외 실매수 제출, 미체결 매수/매도 정리, 가상매수 fallback 로직 이관
- `src/kinvest_trade/liquidity_lab.py`
  - `_place_overseas_test_order()`를 얇은 helper wrapper로 전환

### 결과
- `liquidity_lab.py`
  - 6,346줄 → 5,835줄
- 해외 주문 영역에서 남은 대형 잔존물은 실매도 제출 `_place_overseas_sell_order()` 중심으로 압축됨

### 테스트
- `python3 -m pytest tests/test_liquidity_lab.py -q`
- `python3 -m pytest tests -q`
- 결과
  - `136 passed`
  - `455 passed`

## [2026-07-11] 지시문 #68 7차 반영 - `lab_overseas_orders.py` 1차 분리 (가상 주문/포지션 라우팅)

### 배경
- 6차 분리 이후 `liquidity_lab.py`의 남은 대형 주문 구간은 거의 해외 주문 흐름에 집중되어 있었음
- 그중에서도 아래 3개는 "실주문 제출"보다 "해외 포지션 라우팅/가상체결" 성격이 강해
  먼저 분리하면 실제 해외 주문 메서드와 책임 경계를 더 또렷하게 만들 수 있었음
  - `_manage_overseas_position()`
  - `_record_virtual_overseas_buy()`
  - `_record_virtual_overseas_sell()`

### 수정
- `src/kinvest_trade/lab_overseas_orders.py`
  - `OverseasOrderHelper` 추가
  - 해외 포지션 보유/가상 주문 라우팅 로직 분리
- `src/kinvest_trade/liquidity_lab.py`
  - `OverseasOrderHelper` import 및 `self.overseas_orders` 초기화
  - `_get_overseas_order_helper()` 추가
  - 위 3개 메서드를 얇은 wrapper로 전환

### 결과
- `liquidity_lab.py`
  - 6,777줄 → 6,346줄
- 해외 실주문 제출 메서드와 가상체결/보유량 라우팅 메서드가 분리되기 시작해,
  다음 단계에서 `_place_overseas_test_order()` / `_place_overseas_sell_order()`를 더 안전하게 이관할 기반 확보

### 테스트
- `python3 -m pytest tests/test_liquidity_lab.py -q`
- `python3 -m pytest tests -q`
- 결과
  - `136 passed`
  - `455 passed`

## [2026-07-11] 지시문 #68 6차 반영 - `lab_domestic_orders.py` 분리

### 배경
- 5차 분리 이후에도 `liquidity_lab.py` 안에는 국내 주문 실행 핵심 로직이 크게 남아 있었음
  - `_place_domestic_test_order()`
  - `_place_domestic_sell_order()`
- 해당 메서드들은 주문 제출, 미체결 정정, 알림, 체결 로그, 상태 영속화가 한 덩어리로 묶여 있어
  이후 해외 주문 흐름까지 분리하기 전에 먼저 시장 단위로 경계를 세우는 편이 안전했음

### 수정
- `src/kinvest_trade/lab_domestic_orders.py`
  - `DomesticOrderHelper` 추가
  - 국내 테스트 매수/실매도 주문 플로우를 helper로 이동
- `src/kinvest_trade/liquidity_lab.py`
  - `DomesticOrderHelper` import 및 `self.domestic_orders` 초기화
  - `_get_domestic_order_helper()` 추가
  - `_place_domestic_test_order()`, `_place_domestic_sell_order()`를 얇은 wrapper로 전환

### 결과
- `liquidity_lab.py`
  - 7,438줄 → 6,777줄
- 국내 주문 플로우가 독립 모듈로 분리되어, 다음 단계인 해외 주문 흐름 분리의 기준선 확보

### 테스트
- `python3 -m pytest tests/test_liquidity_lab.py -q`
- `python3 -m pytest tests -q`
- 결과
  - `136 passed`
  - `455 passed`

## [2026-07-11] 지시문 #68 5차 반영 - `lab_watch.py` 확장 (watch target 판정/선택 위임)

### 배경
- 4차 분리 후에도 `liquidity_lab.py` 안에는 아래 watch-target 핵심 로직이 그대로 남아 있었음
  - `_build_watch_target_status()`
  - `_select_domestic_buy_targets()`
  - `_select_domestic_exit_target()`
  - `_select_overseas_buy_targets()`
  - `_remaining_overseas_entry_slots()`
  - `_select_primary_target()`
- 특히 `_build_watch_target_status()`는 테스트가 `liquidity_lab` 모듈의
  `evaluate_entry_setup`, `derive_watch_state`를 monkeypatch 하는 구조와 얽혀 있어,
  단순 함수 이동보다 "service wrapper + helper 위임"이 더 안전했음

### 수정
- `src/kinvest_trade/lab_watch.py`
  - `WatchStateHelper`에 다음 책임 추가
    - watch target 상태 판정
    - 국내/해외 buy target 선택
    - 국내 exit target 선택
    - 해외 진입 슬롯 계산
    - primary target 선택
- `src/kinvest_trade/liquidity_lab.py`
  - `_make_watch_target_status()` 추가
  - `_evaluate_entry_setup()`, `_derive_watch_state()` wrapper 추가
  - 위 메서드들을 helper 위임 wrapper로 전환
  - 결과적으로 `liquidity_lab` 모듈 monkeypatch 테스트 호환성 유지

### 결과
- `liquidity_lab.py`
  - 7,828줄 → 7,438줄
- `lab_watch.py`
  - 494줄 → 1,010줄

### 테스트
- `python3 -m pytest tests/test_liquidity_lab.py -q`
- `python3 -m pytest tests -q`
- 결과
  - 전체 `455 passed`

### 다음 단계
- 남은 대형 복잡도 후보
  - 국내 주문 lifecycle helper
  - 해외 주문 lifecycle / pending order reconciliation helper
  - action summary / report formatting helper
- 다음 분리부터는 `watch`보다는 주문 orchestration 쪽이 더 큰 수익 구간

## [2026-07-11] 지시문 #68 4차 반영 - `lab_watch.py` 분리

### 배경
- `liquidity_lab.py` 3차 분리 후에도 persisted state / signal fallback / strategy restore 관련 보조 로직이
  한곳에 길게 남아 있었음
- 이 영역은 주문 실행 핵심과는 다르지만, watch target 재구성, 재시작 복구, stale 상태 정리 등에
  공통으로 쓰여 파일 상단부 복잡도를 키우고 있었음
- 다만 `_build_watch_target_status()` 본문은 `liquidity_lab` 모듈 함수 monkeypatch 테스트와
  결합되어 있어 그대로 두고, 주변 보조 계층만 먼저 분리하는 편이 안전했음

### 수정
- `src/kinvest_trade/lab_watch.py`
  - `WatchStateHelper` 신설
  - 다음 책임 이동
    - persisted symbol state cache
    - cycle exit reference price priming
    - snapshot payload 복원 / live price 보정
    - 해외 signal cache fallback
    - watch/trade state persistence
    - stale lab position state cleanup
    - strategy context restore
    - watch target cycle log 저장 보조
- `src/kinvest_trade/liquidity_lab.py`
  - `self.watch_state` 초기화
  - 위 메서드들을 helper 위임 wrapper로 전환
  - `_build_watch_target_status()`는 현행 유지
    - 기존 테스트의 `evaluate_entry_setup`, `derive_watch_state` monkeypatch 호환성 보존 목적

### 결과
- `liquidity_lab.py`
  - 8,136줄 → 7,828줄
- 새 파일
  - `lab_watch.py` 494줄

### 테스트
- `python3 -m pytest tests/test_liquidity_lab.py -q`
- `python3 -m pytest tests -q`
- 결과
  - 전체 `455 passed`

### 다음 단계
- 남은 큰 분리 후보
  - `_build_watch_target_status()` 자체와 watch target selection 보조 계층
  - 국내/해외 주문 lifecycle helper
  - report/action summary formatting helper
- 이후 단계는 helper 위임보다 service collaboration object가 더 늘어나는 형태가 자연스러움

## [2026-07-11] 지시문 #68 3차 반영 - `lab_runtime.py` 분리

### 배경
- `liquidity_lab.py` 2차 분리 후에도 운영 상태 관리 로직이 한 파일 안에 길게 남아 있었음
- 특히 아래 책임이 한곳에 섞여 있었음
  - 저매매 빈도 감시
  - RSI 차단 누적 기록
  - `trend_filter_lost` 비율 경고
  - exit cooldown / no_orderable retry 상태
  - event log 저장 보조
- 이 영역은 주문 실행 핵심보다는 런타임 관측/제어 성격이 강해 helper 객체로 묶기 적합했음

### 수정
- `src/kinvest_trade/lab_runtime.py`
  - `LabRuntimeManager` 신설
  - 다음 책임 이동
    - `low_trade_frequency` 누적/알림
    - RSI 차단 카운트 및 이벤트 저장
    - `trend_filter_lost_ratio_high` 감시
    - `exit_cooldown` 관리
    - `no_orderable_retry`, `no_orderable_counts` 관리
    - 공통 event 저장 보조
- `src/kinvest_trade/liquidity_lab.py`
  - `self.runtime` 초기화
  - `_record_cycle_trade_frequency()`
  - `_track_rsi_threshold_blocks()`
  - `_check_trend_filter_lost_ratio()`
  - `_save_event()`
  - `_cooldown_remaining_minutes()`
  - `_defer_no_orderable_position()`
  - `_no_orderable_retry_minutes()`
  - `_track_no_orderable_stall()`
  - `_reset_no_orderable_stall()`
  - `_is_no_orderable_retry_active()`
  - `_clear_no_orderable_retry()`
  - `_register_exit_cooldown()`
  - `_set_exit_cooldown_minutes()`
  를 helper 위임으로 전환
  - `__new__()` 기반 테스트 인스턴스 호환을 위해 fallback runtime config 추가

### 결과
- `liquidity_lab.py`
  - 8,309줄 → 8,136줄
- 새 파일
  - `lab_runtime.py` 436줄

### 테스트
- `python3 -m pytest tests/test_liquidity_lab.py -q`
- `python3 -m pytest tests -q`
- 결과
  - 전체 `455 passed`

### 다음 단계
- 남은 고비용 영역
  - watch target / persisted snapshot / restore 전략 상태
  - 국내/해외 주문 lifecycle helper
  - report/action summary formatting helper
- 이제부터는 단순 이동보다 서비스 협력 객체를 늘리는 방식이 더 적합함

## [2026-07-11] 지시문 #68 2차 반영 - `lab_positions.py` 분리

### 배경
- `liquidity_lab.py` 1차 분리 후에도 파일 길이가 8,649줄로 여전히 매우 큼
- 파일 상단의 `VirtualTradeManager`, `UnifiedPositionTracker`는
  저장소 계층 의존성만 있는 독립 보조 컴포넌트인데도 본 서비스 파일 내부에 남아 있었음
- 이 영역은 별도 테스트가 이미 충분해, 추가 분리 대비 회귀 위험이 낮은 편이었음

### 수정
- `src/kinvest_trade/lab_positions.py`
  - `VirtualPosition`
  - `VirtualTradeManager`
  - `UnifiedPosition`
  - `UnifiedPositionTracker`
  분리
- `src/kinvest_trade/liquidity_lab.py`
  - 위 클래스들을 새 모듈 import로 전환
  - 기존 모듈 경로(`kinvest_trade.liquidity_lab`)에서의 사용 호환은 유지
- `tests/`
  - `test_virtual_trades.py`, `test_unified_position_tracker.py`를 새 모듈 직접 import로 조정

### 결과
- `liquidity_lab.py`
  - 8,649줄 → 8,309줄
- 새 파일
  - `lab_positions.py` 347줄

### 테스트
- `python3 -m pytest tests/test_virtual_trades.py tests/test_unified_position_tracker.py -q`
- `python3 -m pytest tests/test_liquidity_lab.py -q`
- `python3 -m pytest tests -q`
- 결과
  - 전체 `455 passed`

### 다음 단계
- 남은 대형 영역 후보
  - watch target / persisted snapshot / signal fallback 계층
  - 해외 주문 lifecycle / pending order reconciliation 계층
  - report formatting / summary building 계층
- 다음 분리는 서비스 메서드 간 결합도가 더 높아지므로,
  1차/2차처럼 단순 클래스 이동보다 "helper object + service 위임" 방식이 안전함

## [2026-07-11] 지시문 #68 1차 반영 - `lab_risk.py` / `lab_notify.py` 분리

### 배경
- `src/kinvest_trade/liquidity_lab.py`가 8,700줄을 넘기며 연속손절 CB, 일일손실 CB,
  거래 알림 배치 로직까지 한 파일에 몰려 있었음
- 최근 지시문 연속 반영 과정에서 서로 다른 수정이 큰 파일 안에서 겹치며 추적 비용이 커졌고,
  독립 테스트 가능한 최소 단위부터 떼어내는 것이 우선 과제가 됨

### 수정
- `src/kinvest_trade/lab_risk.py`
  - `CircuitBreakerManager` 신설
  - 연속손절/일일손실 CB 상태, 자동 해제, 이벤트 기록, 자동 해제 알림 책임 분리
  - 향후 해외 재진입 쿨다운용 `overseas_allowed()` 상태도 함께 보관
- `src/kinvest_trade/lab_notify.py`
  - `TradeNotifier` 신설
  - 거래 알림 큐, 배치 윈도우, 최대 묶음 건수, flush 포맷 책임 분리
- `src/kinvest_trade/liquidity_lab.py`
  - `self.cb`, `self.trade_notifier` 초기화
  - `_is_trading_halted()`, `_queue_trade_notification()`, `_flush_trade_notifications()`를
    새 모듈 위임으로 전환
  - 기존 테스트와 호출부 호환을 위해 레거시 속성
    (`_consecutive_losses`, `_session_realised_krw`, `_pending_trade_notifications` 등)은
    당분간 동기화 레이어로 유지
  - 국내/해외 실현손익 반영 지점을 `_on_realised()` 헬퍼로 통일

### 테스트
- 신규
  - `tests/test_lab_risk.py`
  - `tests/test_lab_notify.py`
- 검증
  - `python3 -m pytest tests/test_lab_risk.py tests/test_lab_notify.py -q`
  - `python3 -m pytest tests/test_liquidity_lab.py -q`
  - `python3 -m pytest tests -q`
- 결과
  - 전체 `455 passed`

### 다음 단계
- `liquidity_lab.py` 내 remaining candidate
  - 포지션/청산 판단 보조 로직
  - watch target / signal cache 계층
  - 해외/국내 주문 orchestration 보조 함수
- 현재는 동작 보존이 우선이라 레거시 속성 동기화를 남겨두었고,
  다음 분리 단계에서 점진적으로 직접 상태 접근을 제거하는 것이 안전함

## [2026-07-10] 해외 반복 신호 실패 종목 쿨다운

### 배경
- `/lab_report wait 72`와 DB 세부 조회에서 `signal_unavailable`이 특정 해외 종목에 반복 집중됨
- 일부는 구조화 상품/과거 로그였지만, 일반 심볼도 차트 signal 생성 실패가 반복되면
  같은 종목에 API 호출과 WAIT 로그를 계속 쓰는 문제가 남아 있음

### 수정
- `config/fixed_config.json`
  - `overseas_signal_failure_threshold=3`
  - `overseas_signal_failure_cooldown_minutes=180`
- `liquidity_lab.py`
  - 비보유 해외 종목의 signal 생성 실패를 누적
  - 기준 횟수 이상 실패하면 지정 시간 동안 scan pool에서 제외
  - 제외 사유는 `signal_unavailable_cooldown`으로 남김
  - 보유 종목은 청산 감시 보호를 위해 쿨다운 제외 대상에서 제외
- `tests/`
  - 반복 실패 비보유 종목 제외, 보유 종목 계속 감시 회귀 테스트 추가

### 기대 효과
- 신호 생성이 불가능한 종목에 API와 watchlist 슬롯을 반복 소모하지 않음
- `signal_unavailable` WAIT 병목 감소 및 실시간 감시 품질 개선

## [2026-07-10] WAIT 병목 리포트 추가

### 배경
- 최근 72시간 분석에서 `WAIT`의 대부분이 `volume_low`였지만,
  기존 리포트는 시장/전략별 병목을 바로 보여주지 않아 SQL로 직접 확인해야 했음
- 매매 빈도 저하가 안전한 필터 때문인지, 과도한 보수화 때문인지 빠르게 판단하려면
  운영 중 텔레그램에서 병목을 볼 수 있어야 함

### 수정
- `trade_analysis.py`
  - `summarize_wait_bottlenecks()` 추가
  - `WAIT` 로그를 시장, 전략, 사유별로 묶고 평균 `volume_ratio`, RSI, 모멘텀 표시
- `scripts/analyze_trades.py`
  - `--wait-hours`, `--wait-limit` 옵션 추가
- `telegram_control.py`
  - `/lab_report wait [시간]` 명령 추가
- `README.md`, `tests/`
  - 사용법과 회귀 테스트 추가

### 기대 효과
- `/lab_report wait 72`로 저빈도 원인을 즉시 확인 가능
- 향후 `overseas_volume_floor`, `volume_low`, `trend_down` 중 어느 병목이 커지는지 빠르게 판단 가능

## [2026-07-10] 해외 저거래량 전략 진입 차단

### 배경
- 최근 72시간 분석에서 해외 실주문은 순손실이고, 과거 해외 `BUY_REAL` 중
  `[VWAP] volume_low`, `[RSI] volume_low`처럼 낮은 거래량 비율에서도 진입한 기록이 확인됨
- #36의 momentum_policy 이중 게이트 제거는 유지하되, 해외 단타 특성상 최소 유동성 하한은
  별도 안전장치로 남기는 것이 필요하다고 판단

### 수정
- `config/fixed_config.json`
  - `liquidity_lab.overseas_min_strategy_volume_ratio=0.8` 추가
- `liquidity_lab.py`
  - 해외 신규 BUY 전략 신호라도 `volume_ratio < 0.8`이면 `overseas_volume_floor`로 WAIT
  - watchlist 단계, 실주문 직전, 가상매수 직전 3곳에서 동일 기준 적용
  - 차단 시 `cycle_log`/`event_log`에 volume_ratio와 기준값을 남겨 추후 성과 분석 가능
- `tests/`
  - 해외 조합 전략 저거래량 차단, 국내 미적용, 실주문/가상매수 최종 방어 회귀 테스트 추가

### 기대 효과
- 해외 저유동성 구간에서 불필요한 진입과 미체결/슬리피지 리스크 감소
- 국내 전략은 기존처럼 유지해, 최근 상대적으로 양호했던 국내 매매 빈도는 과도하게 줄이지 않음

## [2026-07-10] 전략 전후 비교 시각 기준 지원

### 배경
- 지시문 #65의 `/lab_report compare 2026-07-10`은 KST 0시 기준으로 하루 전체를 나눔
- 7월 10일에는 해외 단독 전략 차단, 보수화, 추가 가드가 같은 날짜 중간에 여러 번 적용되어
  날짜 기준 비교만으로는 “정책 적용 이후” 성과를 정확히 분리하기 어려웠음

### 수정
- `trade_analysis.py`
  - `YYYY-MM-DD` 외에 `YYYY-MM-DDTHH:MM`, `YYYY-MM-DD_HH:MM` KST 컷오프 지원
  - 출력 기준 라벨을 날짜/시각에 맞춰 표시
- `scripts/analyze_trades.py`
  - `--compare-date` 도움말에 시각 기준 사용법 추가
- `telegram_control.py`, `README.md`
  - `/lab_report compare 2026-07-10T18:00` 예시 추가
- `tests/`
  - 시각 기준 컷오프와 텔레그램 명령 회귀 테스트 추가

### 기대 효과
- 정책 변경이 하루 중간에 있었던 경우에도 실제 적용 이후 성과만 분리해 재평가 가능
- 해외 단독 전략 차단 이후 성과와 국내 슬롯 확대 이후 성과를 더 깨끗하게 비교 가능

## [2026-07-10] 가상 포지션 정리 후보 요약 추가

### 배경
- 현재 해외 가상 포지션이 15개로 `max_concurrent_overseas_orders=8`을 초과하고 있어
  신규 해외 진입이 막히는 주요 원인으로 작동
- `/lab_portfolio`는 전체 가상보유 목록은 보여주지만, 초과 상태에서 어떤 종목을 먼저
  점검해야 하는지 손실/노출/보유시간 관점의 요약이 부족했음

### 수정
- `telegram_control.py`
  - 해외 가상 포지션 수가 한도를 초과하면 `─── 가상보유 정리 후보 ───` 섹션 표시
  - 초과 수량과 정리 필요 종목 수를 표시
  - 손실률, 보유시간, 노출금액 기준으로 상위 3개 점검 후보 요약
- `tests/test_telegram_control.py`
  - 포지션 한도 초과 시 정리 후보 섹션이 표시되는지 회귀 테스트 보강

### 기대 효과
- `/lab_portfolio`만 보고도 `/lab_start`로 자동 감시를 재개할지,
  `/lab_reset`으로 초기화할지, 특정 포지션을 우선 확인할지 더 빠르게 판단 가능

## [2026-07-10] 과거 청산 라벨 exit_by 자동 보정

### 배경
- 최근 DB의 `SELL_REAL` 및 `broker_order_events`를 확인한 결과,
  `action_reason=trend_filter_lost`, `stop_loss` 등은 남아 있지만 `exit_by`가 빈 값인
  과거 레코드가 다수 존재
- 현재 신규 주문 저장 경로는 `exit_by = exit_by or exit_reason`으로 보강되어 있으나,
  과거 빈 값이 남아 있으면 `/lab_performance`, `scripts/analyze_trades.py`의 청산 트리거 분석이
  계속 흐려질 수 있음

### 수정
- `repository.py`
  - 스키마 초기화 시 `cycle_log`의 과거 `SELL_REAL` 중 `exit_by`가 비어 있으면
    `action_reason`으로 자동 backfill
  - `broker_order_events`의 과거 매도 주문 중 `exit_by`가 비어 있으면 `reason`으로 자동 backfill
  - 단, 취소 실패/취소 완료처럼 청산 트리거가 아닌 cancel 이벤트는 제외
- `tests/test_repository.py`
  - legacy cycle/broker event의 빈 `exit_by`가 재초기화 시 보정되는지 회귀 테스트 추가

### 기대 효과
- 과거 주문까지 청산 사유별 성과 분석이 더 정확해지고,
  `trend_filter_lost` 등 반복 손실 원인을 전략 리포트에서 더 쉽게 추적 가능

## [2026-07-10] 상태 메시지 시장상태 우선순위 수정

### 배경
- KST 7월 11일 01:53은 미국 정규장 시간대인데 `/lab_status`가
  `시장상태=KRX 휴장`만 표시하는 문제를 확인
- 원인은 `telegram_control.py`의 상태 메시지 로직이 KRX/NYSE 휴장 여부를
  실제 개장/주문가능 시장보다 먼저 판정했기 때문

### 수정
- `telegram_control.py`
  - KRX 정규장, US 주문가능 세션, US 확장 감시 세션을 먼저 표시
  - 양쪽 모두 주문/감시 가능하지 않을 때만 `KRX 휴장`, `US 휴장`, `KRX/US 휴장` 표시
- `tests/test_telegram_control.py`
  - KRX 휴장일이어도 US regular가 열려 있으면 `시장상태=US regular ✓`로 표시되는지 검증

### 기대 효과
- 장 상태 오해를 줄이고, 특히 토요일 새벽 KST의 미국 정규장 상황을 정확히 확인 가능

## [2026-07-10] 지시문 #65 저빈도·RSI 차단 관측 보강

### 배경
- 지시문 #65의 전략 보수화 효과 검증 장치는 구현되어 있었지만,
  `low_trade_frequency`는 로그/DB 이벤트 중심이라 텔레그램 운영 중 즉시 놓칠 수 있었음
- RSI 30 임계값이 너무 강한지 판단하려면 차단 누적 시점의 종목, RSI, threshold가
  DB 이벤트로 남아야 재평가가 쉬움

### 수정
- `liquidity_lab.py`
  - 최근 50사이클 매매율이 1% 미만이면 기존 `low_trade_frequency` 이벤트 저장에 더해
    텔레그램으로 주요 스킵 원인 상위 3개를 요약 경고
  - 저빈도 텔레그램 경고는 200사이클 쿨다운을 적용해 과도한 알림을 방지
  - RSI 임계값 차단 누적 20건마다 `rsi_threshold_blocked` 이벤트를 저장
- `tests/test_liquidity_lab.py`
  - 저빈도 텔레그램 경고 및 쿨다운 회귀 테스트 추가
  - RSI 차단 이벤트 detail 저장 회귀 테스트 추가

### 기대 효과
- 매매가 줄어든 원인이 `overseas_position_cap_reached`, `no_overseas_candidate`,
  `market_not_orderable` 등 무엇인지 텔레그램과 DB 이벤트에서 바로 확인 가능
- RSI 30 기준이 하루 진입 기회를 과도하게 막는지 사후 분석 가능

## [2026-07-10] 가상 포지션 한도 초과 감시중지 경고 보강

### 배경
- 현재 DB 기준 해외 가상 포지션이 15개로 `max_concurrent_overseas_orders=8`을 초과하지만,
  거래 루프가 `stopped`인 상태에서는 자동 청산/정리가 진행되지 않음
- 기존 `/lab_status`와 `/lab_portfolio`는 노출 금액 한도 초과일 때만 `감시=중지` 위험을 강조해,
  포지션 개수 한도 초과만 발생한 경우 사용자가 즉시 조치해야 하는 상황임을 놓칠 수 있었음

### 수정
- `telegram_control.py`
  - `/lab_status`의 가상노출 줄에서 해외 가상 포지션 한도 초과 + 루프 중지 상태면
    `감시=중지`와 `조치=/lab_start 또는 /lab_reset`을 함께 표시
  - `/lab_portfolio` 가상 노출 섹션에서도 포지션 한도 초과 + 루프 중지 상태를 경고하고,
    `/lab_start` 재개 또는 `/lab_reset` 초기화 검토 안내를 표시
- `tests/test_telegram_control.py`
  - 상태/포트폴리오 메시지가 포지션 한도 초과 감시중지 경고를 포함하는지 회귀 테스트 보강

### 기대 효과
- 가상 포지션이 한도를 초과했는데 거래 루프가 멈춰 신규 매수와 자동 정리가 막힌 상태를
  텔레그램에서 즉시 파악하고 조치 가능

## [2026-07-10] 가상거래 리셋 프롬프트에 현재 노출 요약 추가

### 배경
- 현재 DB 기준 해외 가상 포지션이 15개, 가상 노출이 약 `$393,294.92`로
  `max_concurrent_overseas_orders=8`을 초과하고 있음
- `/lab_reset`은 가상 포지션 정리 수단이지만, 실행 전 프롬프트가 삭제 대상만 보여줘
  사용자가 현재 초과 규모와 정리 필요성을 판단하기 어려웠음

### 수정
- `telegram_control.py`
  - `/lab_reset` 프롬프트에 현재 가상보유 노출, 종목 수, 해외 포지션 한도 초과 여부,
    정산대기 건수를 표시
  - 가상보유/정산대기가 없으면 `현재상태=가상보유/정산대기 없음`으로 표시
- `tests/test_telegram_control.py`
  - 빈 상태와 한도 초과 상태의 `/lab_reset` 프롬프트 회귀 테스트 추가

### 기대 효과
- 가상 포지션 한도 초과로 신규 해외 매수가 막힌 상황에서 사용자가 `/lab_reset_confirm`
  실행 여부를 더 안전하게 판단 가능

## [2026-07-10] 미체결 취소 audit 메타데이터 보강

### 배경
- 최근 `both waiting`, `no_orderable_qty`, 미체결 정정/취소 이슈가 반복되면서
  취소 이벤트만으로 원 주문의 가격·수량·주문구분을 복기해야 하는 경우가 늘었음
- BUY/SELL 제출 이벤트는 `order_division`, `reference_price`를 남기도록 보강됐지만,
  stale pending 주문 취소 이벤트는 일부 경로에서 KIS 응답 원문만 저장하거나 이벤트 자체가 누락됨

### 수정
- `liquidity_lab.py`
  - `_broker_cancel_payload()` 추가
  - 자동 stale exit 교체, conflicting sell 취소, stale buy 교체 취소 이벤트에
    `original_order_no`, `order_division`, `original_order_price`, `reference_price`, `open_qty`,
    `response`를 일관되게 기록
  - 오래된 해외 pending BUY를 취소 후 재매수하는 경로에 `stale_buy_replace` broker event 추가
- `telegram_control.py`
  - `/lab_cancel_stale_domestic_confirm`, `/lab_cancel_stale_overseas_confirm` 수동 취소 이벤트도
    동일한 원 주문 메타데이터를 payload에 기록
- `tests/`
  - 자동/수동 취소 성공·거부 경로의 payload 필드 회귀 테스트 보강

### 기대 효과
- `/lab_orders`와 DB에서 미체결 정정/취소 흐름을 주문번호뿐 아니라 가격·수량·주문구분까지 추적 가능
- 기존 주문 때문에 신규 주문이 막힌 상황의 사후 분석 정확도 개선

## [2026-07-10] 지시문 #65 점검 — 빈도 경고 원인 요약 보강

### 실DB 확인
- `scripts/analyze_trades.py data/trading.db --compare-date 2026-07-10`
  - 2026-07-10 KST 이후 국내 VWAP: 15건, 평균 Net `+0.980%`
  - 2026-07-10 KST 이후 해외 RSI: 4건, 평균 Net `-2.025%`
  - 2026-07-10 KST 이후 해외 VWAP: 4건, 평균 Net `-1.011%`
- 최근 2일 기준 국내 실주문접수 SELL_REAL은 평균 Net `+0.341%`,
  해외는 평균 Net `-0.916%`로 국내 비중 확대와 해외 단독 VWAP/RSI 차단 방향이 타당함
- 현재 해외 가상 포지션은 15개로 `max_concurrent_overseas_orders=8`을 초과하여,
  해외 신규 매수 빈도 저하가 포지션 한도 정책 때문일 수 있음

### 확인된 적용 사항
- `/lab_report compare <YYYY-MM-DD>` 기준일 전후 전략 성과 비교 가능
- `max_concurrent_domestic_orders=8`, `_strategy_changes` 메타데이터 적용됨
- 50사이클 매매 빈도 모니터링, RSI 차단 카운터, trend_filter_lost 비율 경고 구현됨

### 추가 수정
- `liquidity_lab.py`
  - `low_trade_frequency` 이벤트 detail에 최근 50사이클의 상위 주문/스킵 이유 `top_reasons` 기록
  - 경고 로그에도 동일 요약을 출력하여 매매 빈도 저하 원인을 바로 분석 가능하게 개선
  - 해외 포지션 한도 초과로 신규 매수가 막힌 경우 `no_overseas_candidate` 대신
    `overseas_position_cap_reached`와 `open_positions/max_positions` 기록
- `telegram_control.py`
  - `/lab_status`, `/lab_portfolio`의 가상노출 요약에 해외 포지션 한도 `현재/최대`와
    초과 여부 표시
- `tests/test_liquidity_lab.py`
  - low frequency 이벤트의 `top_reasons` 기록과 리셋 동작 검증 추가
  - 해외 포지션 한도 초과 시 사이클 리포트 reason이 명확히 남는지 검증 추가
- `tests/test_telegram_control.py`
  - status/portfolio 메시지에 포지션 한도 초과가 표시되는지 검증 추가

### 기대 효과
- 다음 실행에서 매매가 적을 때 단순히 "빈도 낮음"이 아니라
  `volume_low`, `no_candidate`, `cooldown`, `market_not_orderable` 등 원인을 바로 구분 가능
- 보수화 유지/완화 판단을 DB 이벤트만으로 더 빠르게 수행

## [2026-07-10] 매수 주문 audit 메타데이터 보강

### 배경
- 보호성 매도 주문은 `broker_order_events`에 주문 방식과 기준가가 비교적 명확히 남지만,
  매수 주문은 KIS 응답 원문 중심으로만 남아 사후 분석 시 지정가/시장가 여부와 제출 기준가를
  빠르게 확인하기 어려웠음
- 최근 미체결/정정/거부 이슈를 복기하려면 BUY/SELL 양쪽 이벤트 포맷이 일관되어야 함

### 수정
- `liquidity_lab.py`
  - 국내/해외 BUY 실주문 이벤트에 `order_division`, `reference_price`, `response`를 구조화해 기록
  - BUY 주문 결과 dict에도 `order_kind`, `order_division`, `submit_price`, `reference_price` 추가
- `tests/test_liquidity_lab.py`
  - 국내/해외 BUY_REAL 경로가 broker order audit 메타데이터를 남기는지 회귀 테스트 보강

### 기대 효과
- `/lab_orders`와 DB 분석에서 매수 주문의 지정가 제출 여부와 기준가 추적 가능
- 향후 시장가/지정가 정책 변경 시 BUY/SELL 주문 audit 비교가 쉬워짐

## [2026-07-10] 주문 안정 런타임 상태 복원

### 배경
- `exit_cooldown`, `no_orderable_retry`, `no_orderable_counts`는 `LiquidityLabService`
  메모리 상태라 서비스 재시작 시 사라졌음
- 이 경우 `order_rejected`/`no_orderable_qty` 이후 적용한 쿨다운과 백오프가
  재시작 직후 초기화되어 같은 주문 장애를 다시 반복할 수 있었음

### 수정
- `telegram_control.py`
  - `runtime_state.json`에 `lab_runtime_state` 추가
  - 미래 시각의 `exit_cooldown`, `no_orderable_retry`만 저장
  - `no_orderable_counts`는 활성 retry 키에 대해서만 저장
  - 새 `LiquidityLabService` 생성 시 복원 상태를 한 번만 주입
  - 이미 살아 있는 서비스에는 저장값을 다시 덮어쓰지 않아, 정상 해소된 상태가 되살아나는 위험 방지
- `tests/test_telegram_control.py`
  - runtime state 저장 테스트 추가
  - 재시작 후 새 lab service에 쿨다운/백오프가 복원되는지 테스트 추가

### 기대 효과
- 서비스 재시작 후에도 주문거부/매도가능0 재시도 억제 상태 유지
- 장기 고착 주문 장애의 반복 API 호출과 텔레그램/DB 소음 감소

## [2026-07-10] no_orderable_qty 장기 지속 재시도 백오프

### 분석
- 최근 DB 기준 `MSEX`에서 `no_orderable_qty` 스킵 이벤트가 76회 반복 기록됨
- `orderable_qty=0` 장기 지속 상태는 T+2/미체결/브로커 반영 지연 가능성이 높아,
  초기 5분 재시도는 유효하지만 장시간 동일 이벤트를 계속 쌓을 필요는 낮음

### 수정
- `liquidity_lab.py`
  - 초기 `no_orderable_qty` 재시도 간격은 5분 유지
  - stall count 30회 이상이면 20분 재시도
  - stall count 120회 이상이면 60분 재시도
  - `trade_skip` 이벤트 detail에 `retry_after_min` 기록
- `tests/test_liquidity_lab.py`
  - 초기 5분 재시도와 장기 지속 20분/60분 백오프 회귀 테스트 추가

### 기대 효과
- 자본 동결 알림과 추적은 유지하면서, 장기 고착 종목의 DB/API 소음 감소
- `/lab_orders`, event log 분석 시 실제 신규 이슈와 반복 상태를 더 쉽게 구분

## [2026-07-10] 해외 단독 RSI 진입 차단

### 분석
- `scripts/analyze_trades.py data/trading.db --compare-date 2026-07-10` 기준
  - 보수화 이후 해외 `RSI` 단독: 4건, 평균 net `-2.025%`, 승률 `0%`
  - 보수화 이후 해외 `VWAP` 단독: 4건, 평균 net `-1.011%`, 승률 `0%`
  - 보수화 이후 국내 `VWAP`: 15건, 평균 net `+0.980%`
  - 보수화 이후 국내 `VWAP+RSI`: 3건, 평균 net `+1.694%`
- 해석: 국내 VWAP 계열은 유지하고, 해외는 단독 RSI/VWAP보다 복합 확인 신호를 우선해야 함

### 수정
- `config/fixed_config.json`
  - `overseas_block_standalone_rsi: true` 추가
- `liquidity_lab.py`
  - 해외 `strategy_flag == "RSI"` 단독 BUY를 `standalone_rsi_blocked`로 차단
  - `VWAP+RSI`, `VOL+RSI` 같은 복합 전략은 계속 허용
- `tests/`
  - 감시 목록 생성 단계와 주문 직전 단계 모두에서 해외 단독 RSI가 차단되는지 회귀 테스트 추가
- `telegram_control.py`
  - `/lab_guard`에 고정차단(`해외 VWAP단독`, `해외 RSI단독`) 표시 추가

### 기대 효과
- 손실이 컸던 해외 RSI 단독 진입 재발 방지
- 해외 매수는 복합 신호 중심으로 축소하고, 성과가 좋은 국내 VWAP 계열은 유지

## [2026-07-10] 보호성 매도 주문 방식 개선

### 배경
- 손절성 매도는 빠른 청산이 중요한데, 기존 국내/해외 매도 경로는 대부분 지정가 제출에 가까웠음
- 미체결/부분체결/기존 대기 주문이 겹치면 보유 수량이 있는데도 `no_orderable_qty`나 재시도 지연으로 손실 확대가 발생할 수 있었음
- 텔레그램에는 가격만 표시되어 실제 KIS 제출 방식이 시장가인지 지정가인지 추적하기 어려웠음

### 수정
- `liquidity_lab.py`
  - 보호성 청산 사유(`stop_loss`, `atr_hard_stop`, `momentum_loss_cut`, `trend_filter_lost`, `time_exit_loss`)를 별도 주문 스펙으로 분리
  - 국내 보호성 청산은 시장가(`ORD_DVSN=01`, 제출가 0)로 제출
  - 해외 실계좌 보호성 청산은 시장가로 제출
  - 해외 모의투자 보호성 청산은 KIS 모의 안정성을 위해 기준 호가 공격지정가로 제출
  - 손익 계산과 텔레그램 표시는 기준 호가(`reference_price`)를 유지하고, broker audit에는 실제 제출 방식(`order_kind`, `order_division`, `requested_price`)을 함께 기록
- `tests/test_liquidity_lab.py`
  - 해외 모의 보호성 매도는 공격지정가로 남는지 확인
  - 해외 실계좌 보호성 매도는 시장가로 전환되는지 확인
  - 국내 보호성 매도와 미체결 정정 후 재주문이 시장가로 제출되는지 확인
- `README.md`
  - Liquidity Lab 주문 방식 정책 문서화

### 기대 효과
- 손절/추세이탈 같은 보호성 청산의 체결력 개선
- 제출가 0 때문에 손익 로그가 오염되지 않도록 분석 기준가와 실제 제출가 분리
- 텔레그램/DB에서 주문 방식 추적 가능

## [2026-07-10] 텔레그램 제어 세션 ID 복원 개선

### 배경
- 텔레그램 제어 서비스가 재시작되면 `current_cycle_no`, `session_performance`는 복원되지만
  `active_session_id`는 runtime state에 저장되지 않았음
- 이 상태에서 거래 루프가 running으로 복구되면 같은 사용자 세션의 `cycle_log`/`event_log`가
  새 `session_id`로 쪼개져 세션별 손익 분석과 stop 요약의 근거가 약해질 수 있음

### 수정
- `ControllerSnapshot`에 `active_session_id` 추가
- `_write_runtime_state()`에서 현재 세션 ID 저장
- `_restore_runtime_state()`에서 세션 ID 복원
- `_run_cycle()`이 복원된 세션 ID를 `LiquidityLabService._session_id`에 주입하는 회귀 테스트 추가

### 기대 효과
- 서비스 재시작 후에도 같은 텔레그램 거래 세션의 로그와 성과 집계가 이어짐
- `session_start` 이벤트와 세션별 분석이 불필요하게 분리되는 현상 완화

## [2026-07-10] 전략 검증 순손익률 정확도 개선

### 배경
- 지시문 #65 이후 `/lab_report compare`와 `/lab_guard`가 전략 보수화 전후를 판단하지만,
  일부 계산이 `pnl_pct - 0.5%` 고정 비용 추정에 의존하고 있었음
- `cycle_log`에는 이미 `net_pnl_usd`, `net_pnl_krw`, `entry_price`, `qty_executed`가
  저장되어 있어 실제 기록 기반 순손익률 계산이 가능함

### 수정
- `trade_analysis.py`
  - 전후 비교에서 실제 net PnL과 진입 원금이 있으면
    `net_pnl / (entry_price * qty_executed)`를 우선 사용
  - 실제 net 계산이 불가능한 과거 로그만 기존 `pnl_pct - 0.5%` 추정값 사용
- `scripts/analyze_trades.py`
  - 일반 실거래/전략별 분석 출력에 `평균Gross`와 `평균Net`을 함께 표시
- `repository.py`
  - `get_recent_strategy_guard_performance()`도 동일한 실제 net 기반 순손익률을 사용
  - 승률도 gross가 아니라 net 기준으로 계산해 전략가드 판단과 표시를 일치화
- `tests/`
  - 실제 net 컬럼이 있는 경우 고정 비용 추정이 아니라 기록 기반 순손익률을 쓰는 회귀 테스트 추가

### 확인
- `scripts/analyze_trades.py data/trading.db --compare-date 2026-07-10`
  - 보수화 이후 국내 `VWAP` net `+0.980%`, 국내 `VWAP+RSI` net `+1.694%`
  - 보수화 이후 해외 `RSI` net `-2.025%`, 해외 `VWAP` net `-1.011%`
- 해석: 국내 전략 비중 확대와 해외 단독 전략 차단/가드 유지 방향이 실제 net 기준으로도 타당함

## [2026-07-10] 지시문 #61 — 매매 빈도 저하 핵심 원인 수정

### 분석 (07/07~07/10 통합)
- `trend_filter_lost` 비율이 86% / 40% / 57% / 100%로 높아, 진입 직후 노이즈성 손절이 과도했음
- `daily_loss`가 KST 날짜 기준으로 초기화되지 않아 전날 손실이 다음 세션으로 이월되며
  07/10 00:05 KST에 즉시 CB가 발동했음
- `orderable_qty=0`인 해외 포지션이 실제 보유 수량이 있어도 SELL 후보에서 빠져
  자본 동결이 누적됐음
- `daily_loss_limit_pct=1%`는 수수료/단기 변동만으로도 쉽게 초과되어 CB가 과잉 발동했음

### 수정
- `liquidity_lab.py`
  - `__init__`에 `_daily_loss_date` 추가
  - `_is_trading_halted()`에서 KST 날짜 전환 시 `_session_realised_krw`, `_daily_halted_at` 자동 초기화
  - `_select_overseas_exit_targets()`에서 `orderable_qty=0`이어도 `holding_qty > 0`이면
    실보유 수량 기준으로 매도 시도를 허용하고, 실제 주문 실패는 KIS API 응답에 맡기도록 변경
- `config/fixed_config.json`
  - `daily_loss_limit_pct: 0.01 -> 0.02`
  - `tv_min_rel_volume: 2.0 -> 1.8`
  - `overseas_scan_top_n: 22 -> 25`
  - `auto_trade.min_hold_before_trend_exit: 5 -> 12`

### 기대 효과
- 날짜 이월 손실로 인한 장 시작 직후 CB 오작동 제거
- `no_orderable_qty` 반복으로 막히던 해외 매도 재시도 활성화
- 진입 직후 `trend_filter_lost` 노이즈 손절 감소
- TV 후보 풀 확장으로 동일 종목 반복 감시 완화

## [2026-07-06] 지시문 #51 — 전략 분석용 cycle_log 컬럼 보강

### 배경
- 전략 분석에 필요한 `vwap`, `macd_line`, `macd_signal`, `breakout_distance_pct`,
  `atr`, `spread_pct` 등이 `cycle_log`에 누락되어 있었음
- 특히 BUY_REAL 저장 경로는 `signal_snapshot`을 로그에 싣지 않아 진입 시점 기술 지표 분석이 불가능했음
- CB 상태와 보유 기간을 함께 분석할 수 있도록 `consecutive_losses`, `hold_cycles`도 필요했음

### 수정 사항
- `src/kinvest_trade/repository.py`
  - `cycle_log` 스키마에 `vwap`, `macd_line`, `macd_signal`, `macd_golden`,
    `breakout_distance_pct`, `atr`, `spread_pct`, `consecutive_losses`, `hold_cycles` 추가
  - `save_cycle_log()` 시그니처와 INSERT 구문 확장
- `src/kinvest_trade/liquidity_lab.py`
  - `_save_cycle_log_from_watch_target()`에 신규 분석 컬럼 저장 추가
  - 국내/해외 BUY_REAL에 `signal_snapshot` 기반 기술 지표 저장 추가
  - 국내/해외 SELL_REAL에 기술 지표 + `consecutive_losses` + `hold_cycles` 저장 추가
- `tests/`
  - repository 스키마/저장 검증 강화
  - 해외 BUY_REAL 로그에 기술 지표가 실제로 저장되는지 회귀 테스트 추가

### 기대 효과
- 매수/매도/대기 시점의 전략 지표를 DB에서 직접 분석 가능
- CB 카운터와 보유 기간까지 함께 수집되어 사후 성과 해석이 쉬워짐

## [2026-07-06] 해외 quote 전실패 복원력 / paper 설정 분리

### 배경
- 해외 quote API가 한 사이클에서 전부 실패하면 `scan_overseas()`가 held symbol 집합과 signal cache를 같이 비워 보유 종목 감시/청산 문맥이 약해질 수 있었음
- `paper.py`는 dead `risk` 키 제거 후 `auto_trade.max_spread_pct`, `auto_trade.trailing_stop_pct`를 참조하게 되어 paper 테스트와 실거래 전략이 불필요하게 결합된 상태였음

### 수정 사항
- `src/kinvest_trade/liquidity_lab.py`
  - 해외 스캔 결과가 비더라도 held 종목이 있으면 `held_symbols`를 유지해 반환
  - held 종목이 있는 경우 기존 `_signal_cache`와 timestamp cache를 보존해 일시적 quote 장애 시 최근 유효 신호 문맥을 유지
- `src/kinvest_trade/config.py`
  - `PaperConfig`에 `trailing_stop_pct`, `max_spread_pct` 필드 추가
- `config/fixed_config.json`
  - `paper.trailing_stop_pct`, `paper.max_spread_pct` 추가
- `src/kinvest_trade/paper.py`
  - spread / trailing stop 판단을 `config.paper.*` 기준으로 다시 분리
- `tests/`
  - paper 설정 로드 회귀 테스트 추가
  - 해외 quote 전실패 시 held symbol과 signal cache 유지 테스트 추가

### 기대 효과
- 해외 시세 API가 일시적으로 흔들려도 held 종목의 watch/exit 문맥이 더 잘 유지됨
- paper 테스트가 auto_trade 파라미터 변경에 덜 흔들리고 독립성이 회복됨

## [2026-07-06] 지시문 #50 — 현황 검증 후 잔여 개선

### 검증 결과
- 지시문 #47~#49 적용 상태는 정상 확인
- 잔여 이슈는 `risk` dead keys, watchlist 전략 과표시(`RSI`, `VWAP`) 두 축으로 정리됨

### 수정 사항
- `config/fixed_config.json`
  - `risk` 섹션을 실제 사용 중인 4개 키만 남기도록 축소
- `src/kinvest_trade/config.py`
  - `RiskConfig`와 config 로더를 축소된 `risk` 스키마에 맞게 정리
- `src/kinvest_trade/paper.py`
  - 남아 있던 `config.risk.max_spread_pct`, `config.risk.trailing_stop_pct` 참조를 `auto_trade` 설정 참조로 변경
- `src/kinvest_trade/strategy/rsi_macd.py`
  - `is_watching()`에서 단순 `rsi14 <= 55` 조건 제거
  - MACD 골든/상방 유지일 때만 monitoring flag를 표시하도록 정밀화
- `src/kinvest_trade/strategy/vwap_pullback.py`
  - `is_watching()`에서 넓은 RSI 범위 조건 제거
  - VWAP 근접 여부만으로 monitoring flag를 표시하도록 정밀화
- `tests/`
  - 축소된 `risk` 키 집합 검증
  - RSI/VWAP watch 조건 회귀 테스트 추가

### 기대 효과
- 실제로 쓰지 않는 `risk` 키가 설정과 로더에서 제거되어 운영 혼선을 줄임
- watchlist의 `전략=RSI`, `전략=VWAP` 표기가 더 선별적으로 나타나 정보 가치가 올라감

## [2026-07-06] 지시문 #49 — TV 기반 동적 풀 검증 및 개선

### 검증 결과
- TV 동적 풀 갱신 주기, 보유 종목 강제 포함, 신호 캐시, 수동 relist override는 정상 동작 확인
- `tv_min_price_usd`($1)와 `overseas_min_price_usd`($5) 불일치로 TV가 고른 일부 종목이 즉시 제외되고 있었음
- held 종목을 active pool에 보강할 때 `exchange_code`를 항상 `NASD`로 넣어 NYSE 보유 종목에서 KIS 가격 조회 오류 가능성이 있었음
- TV 빈 결과 시 `_tv_available=False`로 영구 전환되어 재시작 전까지 자동 복구가 불가능했음

### 변경 사항
- `config/fixed_config.json`
  - `tv_min_price_usd`를 `5.0`으로 조정해 해외 스캔 가격 필터와 일치시킴
- `src/kinvest_trade/liquidity_lab.py`
  - `_get_held_symbol_map()` 추가로 balance cache에서 `symbol -> exchange_code`를 복원하도록 확장
  - `_active_overseas_pool()`에 `held_symbol_map` 파라미터를 추가해 held 종목 보강 시 실제 거래소 코드를 우선 사용
  - `scan_overseas()`가 `held_symbol_map` 기반으로 active pool과 held symbol set을 구성하도록 변경
  - `_refresh_overseas_dynamic_pool()`에서 TV 빈 결과 시 `_tv_available`을 영구 비활성화하지 않도록 수정
  - 해외 rescan 시 `_tv_diagnostic_ran=False`로 리셋해 다음 주기에 TV 재진단/자동 복구가 가능하도록 보강
  - `_ensure_tv_diagnostics()`는 이미 TV 사용 가능 상태면 재진단을 생략하도록 정리
- `tests/`
  - held 종목 거래소 코드 보존, TV 빈 결과 후 재시도 가능 상태 유지, held symbol map 복원, rescan 진단 플래그 리셋 회귀 테스트 추가

### 기대 효과
- TV 스크리너 결과와 실제 해외 스캔 필터가 일치해 불필요한 종목 폐기가 줄어듦
- NYSE 보유 종목도 정확한 거래소 코드로 가격 조회/감시 가능
- TV 일시 장애 후 서비스 재시작 없이 자동 복구될 수 있는 경로 확보

## [2026-07-06] 지시문 #48 — CB·정책·계산 오류 수정

### 점검 결과
1. 연속손절 CB (30분 자동해제): 정상 동작 확인
2. `daily_loss_limit` CB: 운용 자본 대신 `domestic_min_intraday_turnover_krw`(200억)를 기준으로 써서 발동 임계가 2억원이 되어 사실상 비활성 상태였음
3. 해외 `activity_score`: `change_rate^1.5` 수식 때문에 모멘텀 점수가 지나치게 작아 거래량 편중 선발이 발생했음
4. `slot_max_pct`: `min(slot_entry_pct, slot_max_pct)` 구조상 현재 기본 설정에서는 dead config 상태였음
5. `derive_watch_state()`: `if entry.ready` 분기 양쪽이 동일 반환이라 dead branch였음
6. 국내/해외 SELL 경로의 `_halted_at` 재설정 분기: `_is_trading_halted()` 내부에서 이미 처리되어 dead code였음

### 변경 사항
- `config/fixed_config.json`
  - `risk.operating_capital_krw = 50000000` 추가
- `src/kinvest_trade/config.py`
  - `RiskConfig.operating_capital_krw` 필드 및 로더 연결
- `src/kinvest_trade/liquidity_lab.py`
  - `_is_trading_halted()`의 `daily_loss_limit` 기준을 실제 운용 자본(`operating_capital_krw`)으로 수정
  - `_daily_halted_at`를 도입해 일일손실한도 CB도 30분 쿨다운 후 자동 해제되도록 보강
  - `_scan_single_overseas()`의 `momentum_score`를 `change_rate * 200.0` 선형 스케일로 조정
  - `_slot_based_qty()`를 `slot_entry_pct` 기준으로 단순화
  - 국내/해외 SELL 경로의 `_halted_at` dead code 제거
- `src/kinvest_trade/momentum_policy.py`
  - `derive_watch_state()` dead branch 제거
- `tests/`
  - config 로더와 `daily_loss_limit` CB 발동/자동해제 회귀 테스트 추가

### 기대 효과
- 일일 손실 한도가 실제 계좌 자본 규모에 맞춰 정상 발동
- CB 발동 후 영구 정지처럼 보이던 상태를 자동 복구
- 미장 스캔에서 가격 모멘텀이 종목 랭킹에 실제로 반영
- 불필요한 분기 제거로 코드 추적성과 유지보수성 개선

## [2026-07-06] 보유전략 복원 / 시그널 폴백 / 해외 차트 TTL 캐시

### 배경
- 서비스 재시작 후 held 종목 watchlist가 `전략=-`, `signal_unavailable`로 반복되는 문제가 확인됨.
- `COIN`처럼 이미 수익권에서 `SELL_READY`가 떠야 하는 종목도 평가 실패로 매도 판단을 놓치는 케이스가 발생함.
- 원인상 `watchlist 표시`, `매도 판단`, `재시작 복원`, `과도한 해외 차트 재조회`가 서로 연결되어 있었음.

### 수정 사항
- `repository.py`
  - `lab_symbol_state` 테이블 추가.
  - 종목별 최신 전략/보유 상태/스냅샷을 서버(DB)에 영속 저장하도록 확장.
  - state가 없을 때는 `cycle_log`의 마지막 전략 문맥으로 fallback 복원 가능하게 보강.
- `liquidity_lab.py`
  - 해외 시그널 조회를 `intraday_chart_refresh_sec` 기반 TTL 재사용 구조로 변경.
  - 차트 조회 실패 시 마지막 유효 메모리 캐시 또는 DB 저장 스냅샷으로 fallback 평가하도록 보강.
  - held 종목은 재시작 후에도 DB state를 읽어 strategy manager를 복원하도록 추가.
  - 매수/매도/가상매매 직후 전략 상태를 즉시 state 테이블에 반영하도록 변경.
  - 국내 fluctuation-rank 404가 반복될 때 해당 보조 스캔을 세션에서 자동 비활성화해 로그 폭주를 줄임.
- `telegram_control.py`
  - relist 시 해외 signal cache timestamp도 함께 초기화하도록 보강.

### 효과
- 예기치 못한 종료 후 다음 실행에서도 held 종목의 전략 문맥 복원이 가능해짐.
- 차트 API 일시 실패/호출량 제한 상황에서도 최근 유효 스냅샷으로 매도 판단 지속 가능.
- `signal_unavailable` 때문에 전략 표시와 매도 판단이 동시에 붕괴하던 문제가 완화됨.

## [2026-07-06] 지시문 #47 — 전략 표시 / CB 자동 해제 / 구조 수정

### 발견 문제
- watchlist에서 BUY 직전 종목은 전략 컨텍스트가 비어 `전략=-`로 보였고, `READY` 상태도 `WAIT`로 눌려 보였다.
- 서킷브레이커는 연속 손절 후 발동만 있고 자동 해제 시점이 없어, 재개 전까지 사실상 영구 정지에 가까웠다.
- risk 설정 정리 후보가 있었지만, 확인 결과 `paper.py`가 `max_spread_pct`, `trailing_stop_pct` 등을 실제 사용하고 있어 삭제하면 안 되는 상태였다.

### 수정 사항
- `strategy/*`에 `is_watching()`를 추가해 BUY 전 감시 단계에서도 `VWAP`, `VOL`, `RSI` 전략 컨텍스트가 표시되도록 보강.
- `PriorityStrategyManager`가 HOLD 시에도 monitoring flag를 반환하도록 수정.
- `liquidity_lab.py`에서 watch target의 `action_bias`가 `signal_state`를 그대로 반영하도록 바꿔 `READY`, `WARMUP` 상태를 유지.
- `RiskConfig`와 `fixed_config.json`에 `circuit_breaker_cooldown_minutes`를 연결하고, `LiquidityLabService`에 `_halted_at` 타임스탬프를 도입.
- `_is_trading_halted()`가 쿨다운 경과 후 자동 해제하고 텔레그램 알림을 보내도록 보강.
- 텔레그램 명령 `/lab_cb_reset`을 추가하고, `/lab_resume` 시에도 서킷브레이커 카운터를 초기화하도록 정리.
- `telegram_control.py`의 watchlist 상태 표시를 `READY=📊진입준비`, `WARMUP=⏳준비중`으로 개선.

### 주의 사항
- 지시문 초안에는 risk dead key 제거가 포함되어 있었지만, 실제 코드 사용처 확인 결과 현재는 보류가 맞다.
- 따라서 이번 작업에서는 risk 키를 삭제하지 않고, 쿨다운 설정만 추가했다.

## [2026-07-01] 고정 후보 풀 도입 원인 기록

커밋 7dbafca (2026-06-29): overseas_candidates 74개 최초 정의
커밋 a5603dd (2026-07-01): unified watch 구조 통합 시 완전히 고정 풀로 전환

원인: 국내/해외 activity_score 합산·정렬을 위해 대상 목록 명시 필요.
     KIS API 호출 제한(초당 20회)으로 전체 시장 실시간 스캔 불가.

문제점 (2026-07-03 발견):
  - 풀 불변 -> 오늘의 급등주가 풀 밖이면 감지 불가
  - 저변동성 종목 36% 혼재 (KO, T, VZ, V 등) -> 단타 목표 도달 불가
  - 국내 7종목 중 실제 거래대금 통과 2개만 (삼성전자, SK하이닉스)

거래량 폭증 감지 방식 분석 (2026-07-03):
  - 급등주 거래량: 점진적이 아닌 수직 폭증 (공시 후 1~3분 집중)
  - acml_vol: 체결 즉시 반영
  - FHPST01710000: 1~5분 집계 지연 -> 풀 갱신 전용
  - 현재 25초 polling에서 acml_vol delta로 0~25초 내 폭증 감지 가능

해결 (#39): 2단계 구조
  1단계: FHPST01710000으로 8분마다 후보 풀 갱신
  2단계: 매 사이클 acml_vol delta 추적 -> 5배↑ 시 surge bonus 가산

## 2026-06-30
### 추가 개선 21
- `liquidity_lab.py`의 `_send_summary()`가 사이클 말미에 다시 한 번 체결 요약을 보내며 생기던 중복 알림을 수정
- 실시간 체결 시점에 이미 텔레그램 알림을 보낸 경로(국내 매수, 국내 매도, 해외 매도, 가상 매수/매도)는 반환값에 `already_notified=True`를 담고, `_send_summary()`는 이 플래그를 보면 추가 요약 발송을 건너뛰도록 정리
- 실해외 매수처럼 체결 함수 내부에서 즉시 알림을 보내지 않는 경로는 그대로 `_send_summary()`가 1회만 통보하도록 유지
- `SELL_REJECTED`는 여전히 `_send_summary()`가 유일한 통보 경로이므로 기존처럼 발송되도록 유지
- `tests/test_liquidity_lab.py`에 국내/해외 실거래 중복 차단, 해외 매수 단일 통보 유지, 거부 매도 유지, 전체 `run()` 경로에서 실매도 1건당 알림 1회만 남는 회귀 방지 테스트를 추가

### 추가 개선 20
- `auto_trade` 기본 라벨을 `FIXED_SYMBOL_MOMENTUM`으로 바꾸고, 고정종목 기본 예시를 `SOXL/AMEX`에서 `NVDA/NASD`로 일반화
- `SoxlAutoTrader` 클래스명을 `FixedSymbolAutoTrader`로 바꿔 고정 1종목 전략이 특정 ETF 전용처럼 보이던 오해를 제거
- `cli.py`의 `auto-run` / `liquidity-lab` help 문구를 보강해 `고정 1종목 모드`와 `다종목 자동선정 모드` 차이를 명시
- `main.py` 무인자 실행 주석을 보강해 기본 동작이 `auto_trade.symbol` 고정 감시라는 점을 설명
- `README.md`를 실제 실행 순서 기준으로 전면 재구성하고, `환경 점검 순서`, `운용 모드 비교: auto-run vs liquidity-lab`, 주석 포함 명령 예시, 최신 74개 해외 후보 목록을 반영
- `tests/test_config.py`에 `SOXL` 기본값 제거와 `FIXED_SYMBOL_MOMENTUM` 기본 라벨 검증 테스트를 추가

### 추가 개선 19
- `liquidity_lab` 설정에 `use_slot_sizing`, `slot_entry_pct`, `slot_max_pct`를 추가하고, 국내/해외 실주문 및 해외 가상매수 경로가 고정 1주 대신 주문가능 금액 기반 슬롯 수량을 우선 계산하도록 확장
- 주문가능 금액 조회 실패 시에는 기존 고정 수량으로 자동 폴백하고, 조회는 성공했지만 슬롯 예산으로 1주도 담지 못하는 경우에는 `slot_budget_insufficient`로 안전하게 건너뛰도록 처리
- `config/fixed_config.json` 기본 운영값을 `loop_interval_sec=25`, `overseas_scan_top_n=12`, `liquidity_lab.use_slot_sizing=true`로 조정
- 더 이상 사용되지 않는 `strategy.py`, `risk.py`, `models.py`와 저장소의 `order_intents/orders/positions` 초기 스키마 및 관련 dead method를 제거
- `client.py.environment_division`, `auto_trader.py._hard_break_band_pct`, `technical_signals.py.format_snapshot_indicator`, `liquidity_lab.py._wait_state`를 정리해 남은 로직만 유지
- `tests/test_liquidity_lab.py`에 국내/해외 슬롯 수량 계산과 해외 가상매수 슬롯 적용 테스트를 추가하고, `tests/test_config.py`에 새 설정 필드 검증을 반영

### 추가 개선 18
- `config/fixed_config.json`의 해외 감시 후보군에 `SQQQ`, `TQQQ`, `SOXL`, `SOXS`, `UVXY`를 추가해 인버스/레버리지 ETF도 같은 모멘텀 로직으로 스캔되도록 확장
- `repository.py`에 `virtual_sell_pending` 테이블과 CRUD를 추가해 거래불가 세션에서 실제 보유분을 가상 매도로 먼저 처리한 뒤 정산 대기 상태를 별도로 보존하도록 변경
- `liquidity_lab.py`에 `UnifiedPosition`, `UnifiedPositionTracker`를 추가하고, 실제 보유 수량 + 가상 매수 수량 - 가상 매도 pending 수량을 합산한 통합 포지션 기준으로 청산/추가매수 판단을 재구성
- `_select_overseas_exit_target()`가 종목별 pending 정산 수량을 감안해 이미 가상매도로 처리된 실제 보유분을 같은 세션에서 다시 청산 후보로 고르지 않도록 수정
- `_place_overseas_sell_order()`와 가상 매도 전환 경로를 `apply_sell()` 기반으로 바꿔, 가상 매수분 우선 차감 후 남는 실제 수량만 정산 대기 혹은 실주문으로 연결되게 조정
- 거래 가능 시간이 되면 `_reconcile_pending_virtual_sells()`가 `virtual_sell_pending`을 실제 매도로 정산하고 `[KIS][VIRTUAL_SETTLED]` 알림을 보내도록 추가
- `/lab_virtual` 출력에 정산 대기 매도 수량을 음수 형태로 함께 보여주도록 확장
- `tests/test_unified_position_tracker.py`를 새로 추가하고, 반복 가상매도 방지/정산/가상매수 우선 차감 시나리오를 포함해 전체 테스트를 148개 통과 상태로 갱신

### 추가 개선 17
- 모의투자가 미국 extended session 주문을 거부할 때 별도 저장소에 가상 체결을 남기는 `virtual_positions`, `virtual_orders` 스키마와 `VirtualTradeManager`를 추가
- `liquidity_lab`가 미국장이 열려 있으나 `vps` 환경에서 주문 불가한 경우 실제 주문 대신 `(virtual)` 매수/매도를 기록하고 `[KIS][VIRTUAL_TRADE]` 텔레그램 알림을 보내도록 확장
- 가상 포트폴리오는 실제 브로커 잔고와 분리해 유지하고, 전략 판단에서는 가상 보유분도 exit/watch 대상에 포함되도록 연결
- 텔레그램 컨트롤러에 `/lab_virtual` 명령과 메뉴 등록을 추가해 가상 포트폴리오 보유 현황, 누적 체결 수, 승률, 실현손익을 즉시 조회할 수 있게 함
- README에 거래불가 세션의 가상 체결 정책과 `/lab_virtual` 사용법을 문서화
- `tests/test_virtual_trades.py`를 새로 추가하고, `tests/test_liquidity_lab.py`, `tests/test_telegram_control.py`를 가상 체결 흐름 기준으로 확장

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

## 2026-07-01
### 이번에 수행한 내용
- 자동매매 기본 전략을 `1분 돌파 추종` 중심에서 `5분 눌림목 진입` 중심으로 전환
- `config/fixed_config.json`의 기본 파라미터를 5분봉 기준으로 재조정
  - `intraday_bar_minutes=5`, `intraday_chart_refresh_sec=90`, `poll_interval_sec=30`
  - `intraday_fast_window=10`, `intraday_slow_window=20`, `breakout_lookback_bars=5`
  - `take_profit_pct=0.012`, `full_take_profit_pct=0.020`, `stop_loss_pct=0.005`, `hard_stop_loss_pct=0.010`
  - `volume_spike_ratio=1.5`, `min_intraday_momentum_pct=0.0015`, `min_bar_return_pct=0.0008`
  - `max_entry_rsi14=65.0`, `trend_require_price_above_slow=true`, `max_hold_cycles=120`
- `momentum_policy.py`에 `_pullback_ready()`를 추가하고, 진입 우선순위를 `pullback_entry -> breakout fallback` 구조로 변경
- `time_exit_loss`는 단순 추세 이탈 1개 조건으로 즉시 청산하지 않고, `추세 약화 / 모멘텀 소진 / 거래량 감소` 중 2개 이상일 때만 발동하도록 완화
- 텔레그램/로그 reason 매핑에 `pullback_entry -> 눌림목 진입`을 추가
- 자동매매 시작 메시지의 전략 설명도 `5m pullback` 기준으로 갱신

### 검증 결과
- `python3 -m compileall src` 통과
- `python3 -m pytest tests/test_momentum_policy.py -v` 통과
- `python3 -m pytest tests -v` 통과 (`161 passed`)

## 2026-07-01
### 이번에 수행한 내용
- `liquidity-lab`의 국내/해외 감시 대상을 분리된 풀로 보지 않고 하나의 통합 activity pool로 재구성
- `domestic_top_n` 고정 제한을 제거하고 `unified_watch_top_n`, `unified_scan_top_n` 설정을 추가
- 국내/해외 스캔 결과를 `UnifiedScanResult`로 합산 정렬한 뒤, 상위 후보와 보유 종목을 함께 `watch_targets`에 포함하도록 변경
- 국내 보유 종목은 activity 순위가 낮아도 감시 목록에서 빠지지 않도록 보장
- `run()`에서 더 이상 `한 시장 / 한 종목`만 고르지 않고, 국내와 해외가 동시에 열려 있으면 각 시장의 최선 후보에 독립적으로 진입/청산 가능하도록 분기 재구성
- 리포트/텔레그램용 시장 표기에 `both -> 국내+해외` 매핑을 추가
- API 호출량 추정도 통합 감시 구조에 맞춰 갱신

### 검증 결과
- `python3 -m compileall src` 통과
- `python3 -m pytest tests/test_liquidity_lab.py tests/test_overseas_scan.py tests/test_message_format.py -v` 통과
- `python3 -m pytest tests -v` 통과 (`166 passed`)

## 2026-07-01
### 이번에 수행한 내용
- 24차 점검 결과를 기준으로 `pullback` 판단 로직의 남은 하드코딩을 제거하고, 눌림 거리/RSI 범위/최소 거래량 배율을 모두 `AutoTradeConfig`로 이동
- `config/fixed_config.json`에 `pullback_distance_lower_pct`, `pullback_distance_upper_pct`, `pullback_rsi_low`, `pullback_rsi_high`, `pullback_min_volume_ratio`를 추가
- `_pullback_ready()`가 위 5개 값을 실제로 참조하도록 수정해 설정 변경이 전략 판정에 반영되게 보완
- `overseas_scan_top_n`을 `12`로 되돌려 `unified_watch_top_n=15`와 역할을 분리
- `_send_summary`는 단순 `BUY/SELL` 문자열 가드로 바꾸지 않고, 기존 `already_notified` 구조를 유지
  - 이유: 실해외 매수는 즉시 알림을 보내지 않고 cycle-end summary가 유일한 통보 경로이므로, blanket suppression을 넣으면 알림 누락이 생김
- 대신 `already_notified`가 있는 경로는 중복 전송을 막고, 실해외 매수는 summary로 1회만 전송되는 동작을 테스트로 고정

### 검증 결과
- `python3 -m compileall src` 통과
- `python3 -m pytest tests/test_momentum_policy.py tests/test_liquidity_lab.py -v` 통과
- `python3 -m pytest tests -v` 통과 (`170 passed`)

## [2026-07-03] 지시문 #40 — TradingView 기반 해외 동적 풀 갱신

### 판정 근거
- 국장(KIS): 실시간, 관리종목 제외, 기존 통합 완료 -> KIS API 유지 (#39)
- 미장(TV): 기존 고정 74종목 -> relative_volume_10d_calc 기반 동적 풀로 교체

### 변경 사항
- `tv_scanner.py` 신규 생성: TradingView Scanner API 래퍼
- `config.py`: TV 스캔 파라미터 추가
- `liquidity_lab.py`: 해외 풀 갱신 로직을 TV 스캔으로 대체 (fallback 포함)
- `/lab_relist`는 TV 스캔 결과를 사용자가 수동으로 덮어쓸 수 있도록 유지

### 런타임 분기
- TV 접근 가능: `FHPST01710000(국내) + TV 스캔(해외)` 이중 동적 풀
- TV 접근 불가: `FHPST01710000(국내) + 기존 relist(해외)` (기존 #39 그대로)

## [2026-07-03] 지시문 #41 — datetime 오류 수정 / 휴장일 감지 / GitHub 로그 업로드

### 발생 사고 분석
- 2026-07-03(미국 독립기념일 대체 휴장): 해외 스캔 강행 -> 감시종목 4개로 급감
- cycle=2 부터 datetime timezone mismatch 오류 연속 발생
  원인: `_exit_cooldown` 등에 naive datetime 저장 후 aware datetime과 비교

### 변경 사항
- 코드베이스 전체: `datetime.now()` -> `datetime.now(timezone.utc)` 전수 교체
- `market_calendar.py` 신규: exchange_calendars 기반 NYSE/KRX 휴장 감지
- `liquidity_lab.py`: 휴장일 조기 종료 + 텔레그램 알림
- `git_uploader.py` 신규: GitHub API 로그 업로드
- `telegram_control.py`: `/lab_gitlog` 명령 추가
- `fixed_config.json`: GitHub/휴장 스킵 키 추가

## [2026-07-06] 지시문 #43 — 동시 관리 상향 / 동적 풀 하드코딩 제거 / 고정 감시

### 변경 근거
- `max_concurrent_overseas_orders=3`: 기술적 근거 없는 임의값. 실제 매수는 `[0]`번만 사용하므로 `unified_watch_top_n(20)` 수준으로 상향.
- `overseas_candidates` 74개 하드코딩: TV scan 실패 시 저변동성 종목 fallback으로 단타 전략과 미스매치. 완전 제거.
- 보유종목이 활성 풀 밖이면 watchlist에서 누락되는 갭 발견 -> 강제 포함.

### 변경 사항
- `fixed_config.json`
  - `max_concurrent_overseas_orders: 3 -> 20`
  - `max_concurrent_domestic_orders: 2 -> 5`
  - `overseas_candidates: []`
- `liquidity_lab.py`
  - 실보유 수 체크 후 남은 슬롯만큼만 해외 신규 후보 선택
  - TV 실패 시 fallback 제거 -> 빈 풀 알림 1회 발송
  - `_active_overseas_pool()`은 manual/dynamic만 사용하고, 보유 종목은 강제 포함
  - 보유 종목은 활성 풀 밖이어도 watchlist와 exit 경로에 계속 포함
  - `_awaiting_relist` 상태로 중복 알림 방지
- `telegram_control.py`
  - `_handle_relist()`에서 `overseas_candidates` 참조 제거
  - `SYMBOL:EXCHANGE` 형식 파싱 지원 (`GM:NYSE` 등)

## [2026-07-07] 지시문 #44 — 보유종목 스캔 포함 / 국내 필터 수정 / 서킷브레이커

### 배경
- 전반 점검(2026-07-06)에서 발견된 3가지 미적용 갭 수정.

### 수정 A — scan_overseas 보유종목 pool 강제 포함
- `_active_overseas_pool()`에 `held_symbols: set[str] | None` 파라미터 추가
- `scan_overseas()`에서 `_get_held_symbols()` 호출을 pool 스캔 전으로 이동
- `_active_overseas_pool(held_symbols=held_symbols | _get_virtual_held_symbols())`로 연결
- dynamic_pool 밖 보유종목도 quote scan 대상에 포함되도록 보정

### 수정 B — config
- `domestic_min_intraday_turnover_krw: 500억 -> 200억`

### 수정 C — 서킷브레이커
- `__init__`에 `_consecutive_losses`, `_session_realised_krw` 추가
- `_is_trading_halted()`로 `risk.max_consecutive_losses`, `risk.daily_loss_limit_pct` 반영
- 국내/해외 SELL 완료 후 카운터 갱신
- BUY 선택 직전 halt 체크를 넣어 발동 시 해당 사이클 신규 매수 전체 스킵

## [2026-07-07] 지시문 #45 — 휴장일 relist 알림 차단 / 중복 프로세스 방지

### 발생 사고 (2026-07-04)
- 01:00, 03:30 KST: NYSE 휴장일임에도 자동 relist 알림 발송 (0종목)
- 10:12 KST: `TELEGRAM_CONTROL_START` 메시지 3회 중복 발송
- 05:00 KST `MARKET_STATE_CHANGE from=us_regular`은 이전 세션 상태 잔존에 따른 정상 전환으로 판단

### 수정 사항
- `liquidity_lab.py`
  - `_maybe_send_overseas_relist_alert()`에 `nyse_holiday` 파라미터 추가
  - `run()`에서 `nyse_holiday`를 전달해 휴장일/주말에는 relist 알림 자체를 건너뜀
- `telegram_control.py`
  - `_PID_FILE`, `_acquire_pid_lock()`, `_release_pid_lock()` 추가
  - `run()` 진입 시 PID lock을 획득해 중복 인스턴스를 차단
  - `SIGTERM` 핸들러와 `finally` 해제를 함께 넣어 비정상 종료 후 stale PID 파일 가능성 축소

## [2026-07-07] 지시문 #46 — 코드 구조 점검 기반 개선

### 점검 결과
- `liquidity_lab.py` 내부 dead 함수 3개 확인:
  `_select_domestic_buy_target`, `_select_overseas_buy_target`, `_run_domestic_paper_test`
- `evaluate_scale_in_setup`는 점검 결과 dead code가 아니라 `auto_trader.py`에서 실제 사용 중이라 유지
- 해외/국내 잔고 조회가 동일 사이클 내 중복 호출되는 구간 확인
- 해외 exit 경로의 take profit 기준이 `overseas_take_profit_pct`와 `auto_trade.take_profit_pct`로 갈라져 있던 부분 확인
- `LiquidityLabReport.paper_run` 필드가 항상 skip 값만 담은 채 남아 있어 의미 없는 잔재로 판단

### 변경 사항
- `liquidity_lab.py`
  - dead 함수 3개 제거
  - `_overseas_balance_cache`, `_domestic_balance_cache` 추가
  - 동일 사이클 잔고 재사용으로 국내/해외 잔고 API 중복 호출 감소
  - `_build_exit_setup()`에 `take_profit_override` 추가
  - 해외 보유 포지션 exit 판단 시 `overseas_take_profit_pct`를 일관 적용
  - `LiquidityLabReport.paper_run` 및 관련 하드코딩 skip 제거
- `telegram_control.py`
  - 수동 페이퍼 테스트는 `PaperTradingService`를 직접 호출하도록 정리
  - `paper_run` 누적/요약 참조 제거

## [2026-07-07] 지시문 #52 — 해외 매도 차단 버그 수정 / 다중 exit / watchlist 형식

### 버그 분석
- PFE -2.39% 장시간 미청산 원인:
  `_select_overseas_exit_target()`의 `not overseas_ranked` 조건
  -> `overseas_ranked=[]`일 때 즉시 `None` 반환, fallback 로직 미작동
  -> TV 차단 / KIS 잔고 조회 실패 / relist 미입력 중 하나 발생 시
     stop_loss 초과 포지션이어도 청산 불가
- `1 exit per cycle`은 기술적 제한이 아니라 설계 잔재
- watchlist HOLD 분기는 가격이 빠지고 행 형식이 어긋나는 문제를 만들고 있었음

### 수정 사항
- `liquidity_lab.py`
  - `_select_overseas_exit_targets()` 복수형 추가
  - `overseas_ranked=[]`여도 fallback quote로 TP/SL 및 신호 기반 exit 평가
  - `run()`에서 해외 exit를 `max_exits=5`로 한 사이클에 순차 처리
  - 하위 호환을 위해 `_select_overseas_exit_target()`은 첫 번째 결과만 반환하도록 유지
- `telegram_control.py`
  - `_format_watch_target_line()`의 HOLD 전용 분기 제거
  - `_build_watchlist_message()`에서 `_overseas_balance_cache`를 사용해
    스캔 제외 보유 종목의 손익률을 보완

### 검증 결과
- `python3 -m pytest tests/test_liquidity_lab.py tests/test_telegram_control.py -q` 통과

## [2026-07-07] 지시문 #53 — 매매 성과 분석 기반 개선

### 분석 결과
- 전체 75건 gross 기댓값은 플러스였지만, KIS 왕복 수수료 0.5% 차감 후 net 기댓값은 음수
- 국내 전략은 순기댓값이 유지되었고, 해외 전략 전체가 순손실 구간으로 확인됨
- `marginal_profit_exit` 평균 gross 수익이 수수료 미만이라 순손실 청산의 핵심 원인으로 판단됨
- GitHub 로그 업로드는 타임스탬프 파일명 때문에 동일 일자 데이터가 중복 업로드될 수 있었음
- 서비스 재시작 원인 추적을 위해 최상위 크래시 가시성이 더 필요했음

### 변경 사항
- `fixed_config.json`
  - `liquidity_lab.overseas_take_profit_pct`: `0.012 -> 0.025`
  - `liquidity_lab.overseas_stop_loss_pct`: `0.008 -> 0.015`
  - `auto_trade.min_hold_before_marginal_exit`: `10 -> 30`
- `momentum_policy.py`
  - `marginal_profit_exit`에 `commission_floor = commission_rate * 2 + 0.003` 조건 추가
  - 수수료를 커버하지 못하는 얕은 익절은 조기 청산하지 않도록 보정
- `git_uploader.py`
  - 업로드 파일명을 `YYYYMMDD_session.csv`로 고정해 같은 날 재업로드 시 중복 파일 생성 방지
- `telegram_control.py`
  - 메인 루프 fatal 예외 시 스택 트레이스 `critical` 로깅 추가
  - 텔레그램으로 fatal 예외 요약 알림 전송 후 예외 재상승

### 검증 결과
- `python3 -m pytest tests/test_momentum_policy.py tests/test_git_uploader.py tests/test_telegram_control.py -q` 통과

## [2026-07-08] 지시문 #54 — Virtual position exit 버그 수정

### 발견 사고
- SOLS 가상 매수 포지션이 손절 기준을 크게 초과했는데도 장시간 미청산 상태로 남아 있었음
- 원인: `_select_overseas_exit_targets()` 1차 pass에서
  `remaining_real_orderable <= 0` 조건이 virtual-only 포지션에도 동일 적용됨
- 가상 포지션은 실보유가 없으므로 `remaining_real_orderable=0`이 정상인데,
  이 값 때문에 stop_loss / take_profit 평가 이전에 무조건 skip되고 있었음

### 수정 사항
- `liquidity_lab.py`
  - virtual-only 포지션(`real is None and virtual_buy exists`)은
    `remaining_real_orderable == 0`이어도 exit 평가를 계속 진행하도록 분기 추가
  - `effective_orderable`을 도입해 virtual-only 포지션의 `orderable_qty`에
    `virtual_buy.qty`를 설정
  - 이후 `_place_overseas_sell_order()`가 virtual sell 경로를 정상 진입할 수 있게 연결
- `tests/test_liquidity_lab.py`
  - virtual-only 해외 포지션이 stop_loss 조건 충족 시 exit 후보로 반환되는 회귀 테스트 추가

## [2026-07-08] 지시문 #55 — 가상보유 종목 감시 복구 / TV 자동 복귀

### 발견 문제
- 지시문 #49 이후 `scan_overseas()`가 실보유 심볼만 풀에 강제 포함하고,
  가상보유 심볼은 제외하는 회귀가 발생했음
- 결과적으로 virtual position은 현재가/손익 갱신이 끊기고,
  해외 exit 로직에서도 quote 부재로 처리 품질이 떨어질 수 있었음
- 또한 manual relist가 설정된 상태에서는 `scan_overseas()`가
  `_refresh_overseas_dynamic_pool()`를 다시 호출하지 않아,
  TV가 복구되어도 자동 복귀 로직이 실행될 수 없었음

### 수정 사항
- `liquidity_lab.py`
  - `scan_overseas()`에 `_get_virtual_held_symbols()` 복구
  - `held_symbols`와 `active_overseas_pool` 모두 real + virtual 심볼을 포함하도록 수정
  - 해외 풀 refresh 조건에서 `manual_overseas_pool is None` 가드를 제거해
    rescan 주기마다 manual pool 상태에서도 TV 복구를 재시도하도록 수정
  - `_refresh_overseas_dynamic_pool()`에서
    manual pool + TV available 시 TV 스캔 재시도
    성공하면 manual pool 자동 해제 후 TV 동적 풀로 복귀, 텔레그램 알림 발송
- `tests/test_overseas_scan.py`
  - virtual held symbol이 스캔 풀과 signal cache에 복구되는 테스트 추가
  - manual pool 상태에서 TV 복구 시 자동 전환되는 테스트 추가
  - manual pool 상태에서도 rescan 시 dynamic refresh가 재호출되는 테스트 추가

## [2026-07-08] 지시문 #56 — ConnectTimeout 크래시 방지

### 사고 내용
- `/lab_start` 직후 KIS token 요청에서 `httpx.ConnectTimeout`이 발생하면
  `ensure_token()`에서 예외가 그대로 전파되어 서비스가 fatal 종료될 수 있었음
- 원인: `ensure_token()`은 `_request()` 재시도 루프 바깥에서 직접 `httpx.post()`를 호출하고,
  자체 재시도나 예외 래핑이 없었음

### 수정 사항
- `client.py`
  - `httpx.AsyncClient` timeout을 `connect/read/write/pool = 10s`로 상향
  - `ensure_token()`에 `httpx.HTTPError` 기준 3회 재시도 추가
  - 최종 실패 시 `KisApiError("token_request_failed: ...")`로 래핑해 상위에서 일관 처리
- `liquidity_lab.py`
  - 기존 `run()` 본문을 `_run_cycle()`로 분리
  - `run()`에서 `KisApiError`, `httpx.ConnectTimeout`, `httpx.NetworkError`, `httpx.ReadTimeout`
    을 잡아 `primary_selection_reason="network_error"` 빈 보고서로 사이클 스킵 처리
  - 일시적 네트워크 오류가 서비스 크래시로 번지지 않도록 완충
- `tests`
  - token 요청 timeout 재시도 성공/실패 테스트 추가
  - `LiquidityLabService.run()`의 network_error fallback 테스트 추가

## [2026-07-08] 지시문 #57 — Portfolio 가상보유 현재가 표시 / TV 저거래량 fallback

### 진단 결과
- 가상보유 종목 스캔 자체는 지시문 #55로 복구되어 정상 동작 중이었음
- 남은 문제는 `telegram_control.py`의 `_build_portfolio_message()`가
  가상보유 섹션에서 평균단가만 표시하고 현재가/손익을 렌더링하지 않는 것이었음
- 또 TV 동적 풀은 거래량 기준이 빡빡한 날 결과가 지나치게 적어
  보유 종목 위주 watchlist만 남는 경우가 있었음

### 수정 사항
- `telegram_control.py`
  - portfolio용 `price_lookup` 추가: `watch_targets`, 실보유 `current_price`,
    `_overseas_balance_cache` 순으로 현재가 보완
  - 가상보유 섹션에 `매입 / 현재 / 손익` 표시 추가
  - 현재가를 찾지 못하면 `(현재가 없음)` 문구 유지
- `liquidity_lab.py`
  - TV 스캔 호출을 helper로 정리
  - 결과가 `tv_top_n * 0.3` 미만이면 `min_rel_volume * 0.6`으로 완화 재시도
  - 이 fallback은 일반 TV 갱신과 manual pool 자동 복귀 경로 모두에 동일 적용
- `tests`
  - portfolio 가상보유 현재가/손익 표시 테스트 추가
  - TV 저거래량 fallback 재시도 테스트 추가

## [2026-07-08] 알림 집계 + 주문 응답 감사로그 보강

### 확인된 현상
- 한 사이클에 복수 종목 매매가 발생해도 텔레그램에서는 일부만 보이는 것처럼 보일 수 있었음
- 원인 1: 실제 코드는 복수 주문을 처리하지만, 사용자가 보는 알림은 주문별 즉시 발송과 사이클 요약이 혼재되어 추적성이 떨어졌음
- 원인 2: 해외 실매수는 summary 경로 의존, 국내/매도/가상매매는 즉시 알림 경로 의존이라 경로가 비대칭이었음
- 추가 확인: 현재 해외/국내 실주문은 모두 `order_division="00"` 지정가이며, 코드상 `0.000` 주문가를 의도적으로 보내는 경로는 없었음

### 수정 사항
- `liquidity_lab.py`
  - 성공한 매수/매도/가상매매 알림을 `[KIS][LAB_TRADE_BATCH]` 형태로 집계하도록 변경
  - 기본 60초 윈도우 또는 일정 건수 이상 시 묶어서 발송
  - 테스트에서는 즉시 flush 가능하도록 window 0 지원
  - stop/terminate 시 미발송 집계 알림 강제 flush 추가
  - 주문 응답에서 broker order no를 추출하는 helper 추가
  - 실주문/가상주문 모두 broker audit 저장 연동
- `repository.py`
  - `broker_order_events` 테이블 추가
  - 요청 수량/가격, 전략 태그, 실/가상 여부, broker order no, raw payload 저장
- `telegram_control.py`
  - `/lab_stop`, `/lab_terminate` 시 pending trade batch 강제 발송
- `tests`
  - broker order event 저장 테스트 추가
  - batch notification 포맷 반영 후 전체 테스트 통과 확인

## [2026-07-08] 지시문 #58 — 종목명 / 알림 형식 / 쿨다운 / A/B 아키텍처

### 분석 (07/08 세션 22.8h, 49건)
- 승률 36%, 손익비 4.44, Gross +0.47%, Net -0.03%
- 해외 Net +0.29% (개선 중), 국내 Net -0.51% (계속 문제)
- FHTX +3.5%, PLBL +3.7%, CMPS +2.88% — TP 2.5% 효과 확인
- SKIP 57건: CMPS RSI=81.5 재진입 차단 = 정상
- 거래 공백 4.1h(US마감~KRX개장) = 정상, 11.1h = 시스템 미실행 (CB+크래시)
- 6% 초과 손실: 이 세션 없음, 이전 WULF/SOLS는 virtual/T+2 문제
- RIVN 재매수: exit_cooldown=8분으로 짧아 손절 직후 재진입 허용됨

### 수정
- `DomesticScanResult`에 `stock_name` 필드 추가
- `_refresh_domestic_dynamic_pool()`에서 `_dynamic_domestic_names` 저장
- `_scan_single_domestic_quote()`, `_scan_single_domestic()`에서 이름 주입
- `telegram_control.py`에 `_format_symbol_label()` 추가, 국내 watchlist/positions/portfolio에 종목명 표기
- 거래 알림 헤더를 `[KIS][거래알림]`으로 통일
- 사이클 summary에 SKIP/주문거부 건수와 상위 사유 포함
- `_register_exit_cooldown()`을 손절 25분, 추세이탈 12분, 미미익절 15분, 기본 8분으로 조정

### 설계 문서 (구현 보류)
- 복수 정책 A/B 아키텍처: `shadow_trade_log` 스키마 설계
- `PolicyOrchestrator` 인터페이스 정의
- `/lab_policy_compare` 리포트 형식 정의

## [2026-07-08] 지시문 #59 — gitlog 기록 정보 재구성

### 진단 결과
- 기존 `logs/trades/YYYYMMDD_session.csv`는 WAIT/HOLD가 대부분이라 실거래 분석에 노이즈가 컸음
- 실거래 행에도 `entry_price`, `qty_executed`, `net_pnl`, `commission`,
  `is_virtual`, `orderable_qty`, `stock_name`, `hold_duration_min`, `cb_active`,
  `pool_size` 같은 핵심 정보가 비어 있었음
- CB 발동/해제, session crash, TV 스캔/풀 갱신, 쿨다운 차단 같은 시스템 이벤트는
  별도 테이블이 없어 사후 원인 추적이 어려웠음

### 변경 사항
- `repository.py`
  - `event_log` 테이블 및 `save_event()` / `list_event_log()` 추가
  - `cycle_log`에 실거래 분석용 컬럼 확장:
    `entry_price`, `qty_executed`, `net_pnl_*`, `commission_*`, `is_virtual`,
    `orderable_qty`, `stock_name`, `hold_duration_min`, `entry_time`,
    `exit_cooldown_remaining`, `cb_active`, `pool_size`
- `liquidity_lab.py`
  - BUY_REAL / SELL_REAL 저장 시 수수료, 순손익, 주문가능수량, 보유시간,
    CB 상태, 풀 크기, `activity_score`까지 함께 저장
  - 주문 거부/예산 부족/신호 부족은 `SKIP` cycle_log + `trade_skip` event로 동시 기록
  - `session_start`, `session_crash`, `cb_fired`, `cb_released`,
    `tv_scan`, `pool_refresh`, `cooldown_blocked` 자동 기록
- `git_uploader.py`
  - 단일 session CSV 대신
    `logs/trades/YYYYMMDD_trades.csv` 와 `logs/events/YYYYMMDD_events.csv` 로 분리 업로드
  - 공통 `_upload_csv()` helper 추가
- `telegram_control.py`
  - `/lab_gitlog` 결과 메시지를 trade / event 파일별로 구분해 표시

## [2026-07-09] 최근 거래 로그 점검 기반 개선

### 확인된 문제
- 국내 `time_exit_profit` 청산 중 일부가 gross 기준으로는 수익이지만
  비용 차감 후 `net_pnl_krw < 0` 상태로 기록됨
- 원인: `liquidity_lab.py`가 국내/해외를 동일 `commission_rate=0.25%`로 계산해
  국내 비용을 과대 추정했고, 해외는 `sec_fee_rate` / `fx_fee_rate`를 실현손익 계산에 반영하지 않았음
- 국내 `orderable_qty=0` 보유 종목이 `SELL_READY -> SKIP(no_orderable_qty)`로 반복되어
  거래 로그와 이벤트 로그에 불필요한 노이즈가 누적됨
- watchlist/상태 로그에서 `BUY`인데 메모가 `[VWAP] volume_low`처럼 보이는 모순이 존재했음

### 적용한 개선
- `config.py`, `config/fixed_config.json`
  - `domestic_commission_rate`, `overseas_commission_rate`, `domestic_sell_tax_rate` 추가
- `liquidity_lab.py`
  - 국내/해외 비용 계산 helper 분리
  - 해외 순손익에 `sec_fee_rate`, `fx_fee_rate` 반영
  - `time_exit_profit`, `marginal_profit_exit`, `partial_profit_lock` 등
    수익형 청산은 비용 차감 후 순손익이 0 이하이면 매도 보류
  - 국내 `orderable_qty=0` 포지션은 실제 매도 대상 선택에서 제외
  - 전략 BUY 신호가 legacy entry_setup과 충돌할 때 메모를
    `strategy_buy_signal` 쪽으로 정리

## [2026-07-09] 지시문 #60 — CB 우회 / 동일 종목 중복 매수 / 시장 세션

### 07/09 세션 분석
- 실거래 기준 승률이 낮고 CB가 반복 발동했는데, 로그상 `cb_active=1` 상태에서도 BUY가 일부 진행됨
- 원인 1: consecutive CB 자동 해제 직후 `_is_trading_halted()`가 즉시 `False`를 반환해
  `daily_loss_limit` CB 체크를 우회
- 원인 2: `_select_overseas_buy_targets()`가 이미 보유 중인 종목을 BUY 후보에서 제외하지 않아
  동일 심볼 연속 매수가 가능
- 원인 3: `no_orderable_qty` 포지션이 여러 사이클 연속으로 반복 SKIP되며 이벤트 로그 노이즈 발생
- 원인 4: 미국 `daytime` 세션 시간이 KIS 공식(KST 09:00~15:00 DST / 16:00 non-DST)과 불일치

### 수정
- `liquidity_lab.py`
  - consecutive CB 자동 해제 후 `daily_loss_limit` 체크로 fall-through 되도록 수정
  - `_select_overseas_buy_targets(..., held_positions=...)` 추가
  - 실보유/가상보유 종목은 해외 BUY 후보에서 제외
  - `no_orderable_qty`는 5분 재시도 버킷으로 관리하고 최초 시점에만 상세 이벤트 기록
- `market_sessions.py`
  - US daytime 세션을 KIS 공식 시간으로 보정
- `tests`
  - CB 해제 후 daily_loss가 여전히 매수를 막는지 테스트 추가
  - 보유 중 심볼이 해외 BUY 후보에서 제외되는지 테스트 추가
  - daytime 세션 시간 변경 반영

## [2026-07-10] 미체결 해외주문 재처리 / 공격적 지정가 개선

### 원인 확인
- `ALNY`는 모의계좌 잔고상 `보유 61주`, `ord_psbl_qty=0`으로 조회되었고,
  주문체결내역 기준 `2026-07-10 00:56 KST` 매도주문 `ODNO=52821`
  `337.57 USD`, `미체결수량 61` 상태가 유지되고 있었음
- 즉 `no_orderable_qty`는 계산 버그보다 `미체결 지정가 매도`로 수량이 잠긴 케이스였음
- 기존 해외 주문은 매수/매도 모두 `last_price` 기준 지정가로 제출되어,
  급변 구간 손절 체결력이 낮았음

### 적용한 개선
- `client.py`
  - 해외 주문체결내역 조회 `inquire-ccnl`
  - 해외 정정취소주문 `order-rvsecncl` helper 추가
- `liquidity_lab.py`
  - 해외 매수 실주문 가격을 `ask` 기준으로 조정
  - 해외 매도 실주문 가격을 `bid` 기준으로 조정
  - 손절/추세이탈 계열 보호성 매도는
    `기존 미체결 매도 감지 -> 45초 이상 경과 시 취소 -> bid 재주문`
  - 일반 해외 매수는 최근 미체결 매수 주문이 있으면 중복 매수 차단,
    120초 이상 경과한 주문만 취소 후 재주문
  - 같은 종목의 반대 방향 미체결 주문도 충돌 주문으로 간주:
    BUY 전 기존 SELL 미체결, SELL 전 기존 BUY 미체결을 감지하고
    주문을 방해하면 취소 후 현재 의도에 맞는 주문으로 교체
  - 정정취소 이벤트를 broker_order_events에 `CANCELED`로 기록
- `tests`
  - stale pending sell 취소 후 재주문 테스트 추가
  - 최근 pending buy 중복 방지 테스트 추가
  - 반대 방향 충돌 주문 취소 후 재주문 테스트 추가
  - 해외 매도 가격/손익 계산이 bid 기준으로 반영되도록 기대값 갱신

## [2026-07-10] PCAP 허위 매도거부 알림 수정

### 원인 확인
- `PCAP`는 실제로 분할 매도가 진행되고 있었는데, `time_exit_profit` 같은
  수익 청산 경로에서 `net_profit_below_cost` 검사가 주문 **이후**에도 다시
  실행되고 있었음
- 그 결과 실주문은 이미 제출됐는데 함수 반환값이 `skipped=True`로 덮여
  텔레그램 요약 알림이 `매도거부`처럼 잘못 표기될 수 있었음
- 배치 주문에서 일부 종목만 미실행된 경우에도 summary가 이를 `주문거부`로
  뭉뚱그려 보여 실제 체결과 체감이 어긋났음

### 적용한 개선
- `liquidity_lab.py`
  - 국내/해외 `time_exit_profit`, `take_profit` 계열 청산의
    `net_profit_below_cost` 검사를 주문 제출 **전**으로 이동
  - 이미 알림된 실제 체결이 있는 배치 사이클에서 남은 skip 건은
    `동작=추가미실행`, `미실행=N건` 형태로 표기하도록 변경
  - batch root 대신 leaf order 기준으로 대표 주문을 고르는 summary 보강
  - 해외 미체결 취소 후 재매도 시 `참고=미체결 매도 정정 후 재주문`,
    반대방향 충돌 주문 정리 시 `참고=미체결 매수 취소 후 재매도` 표기
- `tests`
  - 국내 수익청산 보류 시 실제 주문이 나가지 않는지 회귀 테스트 추가
  - 해외 수익청산 보류 시 실제 주문이 나가지 않는지 회귀 테스트 추가
  - 이미 체결 알림이 있는 배치에서 skip 건이 `추가미실행`으로 표기되는지
    summary 테스트 갱신

### 검증
- `python3 -m pytest tests -q`
  - `311 passed`

## [2026-07-10] 지시문 #62 — order_rejected(both waiting) 수정 / 전략 개선

### order_rejected 분석
- ALNY 52분 방치의 직접 원인은 `orderable_qty=0` 상태에서
  held quantity fallback으로 실매도를 계속 재시도한 데 있었음
- KIS 내부 pending 매도 주문이 정리되지 않은 상태에서 추가 SELL이 들어가며
  `Both-sided waiting order exists` 계열 `order_rejected`가 반복될 수 있었음
- BBIO처럼 `orderable_qty=0`가 길게 유지되는 종목은 장시간 자본이 묶일 수 있음

### 수정 사항
- `_place_overseas_sell_order()`
  - `order_rejected` 발생 시 20분 exit cooldown 즉시 등록
  - event log에 cooldown 적용 사실 함께 저장
- `_select_overseas_exit_targets()`
  - `orderable_qty=0` + exit cooldown 활성화 상태면 held quantity fallback 재시도 차단
  - 장기 `no_orderable_qty` 종목은 사이클 카운트를 누적하고 30사이클 시 텔레그램 경고 발송
- `PriorityStrategyManager`
  - `VWAP` 단독 진입은 `vwap_min_price_above_pct` 이상일 때만 허용
  - `RSI` 단독 진입은 `rsi_entry_threshold` 이하일 때만 허용
- `config/fixed_config.json`
  - `vwap_min_price_above_pct=0.003`
  - `rsi_entry_threshold=35.0`

### 검증 포인트
- `Both-sided waiting` 류 거부가 반복되더라도 동일 종목은 20분간 재시도하지 않음
- `orderable_qty=0` 장기 지속 종목은 텔레그램 경고로 조기 식별 가능
- `VWAP 단독`, `RSI 단독` 진입은 기존보다 보수적으로 제한됨

## [2026-07-10] 국내 매도거부 반복 억제 / exit_by 분석 컬럼 추가

### 추가 점검 결과
- 최근 `cycle_log`에서 국내 `069500`, `379800` 매도도 `sell:order_rejected`가
  발생한 흔적이 확인됨
- 해외처럼 반복 폭주까지는 아니지만, 같은 pending/처리 지연류 거부가 국내에서도
  연속 재시도로 이어질 수 있음
- `cycle_log`에는 `strategy_flag`, `entry_by`는 있었지만 `exit_by`가 없어
  청산 전략별 성과 분석이 끊겼음

### 수정 사항
- 국내 매도 `order_rejected` 발생 시 10분 exit cooldown 등록
- 국내 exit target 선정 시 cooldown 중인 종목은 매도 재시도 대상에서 제외
- `cycle_log.exit_by` 컬럼을 런타임 마이그레이션에 추가
- SELL/WAIT/SKIP 로그에 가능한 경우 `exit_by`를 함께 저장

### 기대 효과
- 국내 매도 주문 거부가 반복 알림/반복 주문으로 번지는 현상 감소
- 다음 성과 분석에서 `entry_by`, `exit_by`, `action_reason` 조합별 손익 분석 가능

## [2026-07-10] 해외 단독 VWAP/RSI 신규진입 보수화

### 분석 근거
- `cycle_log` 집계 기준 해외 `VWAP` 단독과 `RSI` 단독 SELL_REAL의 순손익이
  가장 크게 음수로 나타남
- 반면 국내 `VWAP`, 국내 `VWAP+RSI`, 해외 복합 신호(`VWAP+RSI` 등)는
  상대적으로 성과가 양호하거나 표본상 방어력이 있었음

### 수정 사항
- 해외 신규 매수에서 `strategy_flag`가 정확히 `VWAP` 또는 `RSI` 단독인 경우,
  `evaluate_entry_setup()`도 동시에 `ready=True`일 때만 BUY로 통과
- 단독 전략 신호만 있고 거래량/추세/돌파 확인이 부족하면
  `WAIT`, note=`[전략] confirm_wait:<reason>`으로 표시
- 국내 신호와 해외 복합 신호는 기존 속도를 유지

### 기대 효과
- 해외 단독 후행/과매도 신호의 과잉 진입 감소
- 성과가 비교적 좋았던 복합 신호 위주로 신규 해외 진입 압축

## [2026-07-10] 해외 이상호가 청산 보호 / 가상 정산대기 정리

### 추가 점검 결과
- `virtual_orders`에서 PLBL이 10달러 부근에서 6.42달러로 급변한 가격에
  `atr_hard_stop` 가상 손절된 기록이 확인됨
- 해당 패턴은 실제 급락일 수도 있지만, 단일 비정상 호가나 stale/daytime quote가
  가상 포지션을 허위 손실로 삭제할 위험이 있음
- `virtual_sell_pending`에는 실제/가상 보유가 모두 없는 MSEX 정산대기 522주가
  남아 있어 포트폴리오 정산대기 표시를 오염시킬 수 있었음

### 수정 사항
- 해외 청산 대상 선정 전 가격 보호 게이트 추가
  - 직전 저장가 대비 20% 초과 급변 시 첫 사이클은 `price_shock_confirm`으로 보류
  - 다음 사이클에서도 같은 가격대가 확인되면 실제 급락으로 보고 청산 허용
  - last price와 bid/ask mid가 3% 이상 어긋나면 `price_mid_mismatch`로 청산 보류
- 가상 정산대기 reconciliation 개선
  - 부분 정산 시 정산대기를 통째로 삭제하지 않고 잔량만 유지
  - 실제 보유와 가상 보유가 모두 없는 정산대기는 `virtual_pending_cleanup` 이벤트로 삭제

### 기대 효과
- 단일 이상호가로 인한 대형 가상손실 기록 방지
- 정산대기 잔재가 포트폴리오와 다음 판단을 오염시키는 현상 감소

## [2026-07-10] stale lab position 상태 자동 정리

### 추가 점검 결과
- `lab_symbol_state`에 7월 6~9일의 과거 `has_position=1` 상태가 남아 있어
  실제/가상 보유가 아닌 종목이 watchlist와 전략 복구 문맥을 오염시킬 수 있었음
- 이 상태는 포트폴리오의 실보유/가상보유 계산보다 watchlist, cached signal,
  restart 후 strategy context 복구에 더 큰 영향을 줄 수 있음

### 수정 사항
- `SqliteRepository.clear_stale_lab_positions()` 추가
  - 현재 활성 포지션 목록에 없는 `has_position=1` 행만 `has_position=0`,
    `holding_qty=0`, `note=stale_position_cleared`로 갱신
- `LiquidityLabService._clear_stale_lab_position_states()` 추가
  - KIS 잔고 조회가 성공한 시장만 대상으로 stale state 정리
  - 일시적 API 실패/미조회 시장은 건드리지 않음
  - 정리 결과는 `lab_position_state_cleanup` event로 저장

### 기대 효과
- 오래된 포지션 상태가 watchlist/전략 복구를 오염시키는 현상 감소
- 재시작 후 실제 보유가 아닌 과거 종목에 대한 SELL_READY/HOLD 표시 감소

## [2026-07-10] 해외 포지션 한도에 가상보유 반영

### 추가 점검 결과
- 해외 신규 매수 한도 계산에서 실제 보유 수만 차감하고 가상 보유 수는
  충분히 차감하지 않는 구조가 확인됨
- 이미 가상 포지션이 15개 쌓인 상태에서도 `max_concurrent_overseas_orders`
  잔여 슬롯이 과대 계산될 수 있어 세션 외 가상 포지션 누적 위험이 있었음

### 수정 사항
- `_remaining_overseas_entry_slots()` 추가
  - 실제 보유와 가상 보유를 합산하되 동일 심볼은 1개 포지션으로 중복 제거
- 해외 신규 매수 선정 시 남은 슬롯을 `monitored_overseas_positions`
  기준으로 계산하도록 변경

### 기대 효과
- 가상 보유가 많은 상태에서 신규 해외 진입이 과도하게 누적되는 현상 감소
- `max_concurrent_overseas_orders`가 실보유+가상보유 통합 한도로 동작

## [2026-07-10] 가상 해외 총노출 예산 제한

### 추가 점검 결과
- 현재 가상 해외 포지션 명목금액이 약 39만 달러까지 누적되어 있었음
- 가상매수는 실제 계좌 현금이 줄지 않기 때문에 KIS 주문가능금액만 기준으로
  sizing하면 포지션이 누적될수록 총 노출이 과대해질 수 있음

### 수정 사항
- `liquidity_lab.max_virtual_exposure_pct` 설정 추가 (기본 1.0)
- `_open_virtual_overseas_notional()` / `_remaining_virtual_overseas_budget()` 추가
- 가상 해외 매수 시 기존 가상 보유 명목금액을 차감한 남은 예산 안에서만
  slot sizing 수행
- 남은 가상 예산이 없으면 `virtual_exposure_limit`으로 신규 가상매수 스킵

### 기대 효과
- 모의투자 정규장 외 시간대에 가상 포지션이 계좌 규모 이상으로 누적되는 현상 억제
- 가상 성과와 실제 운용 가능 규모 간 괴리 감소

## [2026-07-10] 해외 unit/warrant/right 특수증권 필터

### 추가 점검 결과
- 최근 로그에서 `CXIIU` 같은 unit 계열 티커가 해외 동적 풀 후보로 유입된 흔적이 확인됨
- 이런 티커는 일반 보통주보다 유동성/가격 구조가 특수해 단타 테스트 대상으로 부적합함

### 수정 사항
- `_overseas_speculative_reasons()`에 구조화 티커 필터 추가
  - 5글자 이상 `U` 종료: `structured_unit_symbol`
  - 5글자 이상 `WTS/WS/WT/W/RT/R` 종료: `structured_warrant_or_right_symbol`
- `BIDU`처럼 4글자 일반 티커는 오탐하지 않도록 회귀 테스트 추가

### 기대 효과
- TradingView 동적 풀에서 unit/warrant/right 계열 특수증권 신규 진입 차단
- 저품질·특수 구조 티커로 인한 예외/급변 리스크 감소

## [2026-07-10] 포트폴리오 가상 노출 요약 추가

### 추가 점검 결과
- 가상 해외 총노출 제한을 추가했지만 `/lab_portfolio`에서는 현재 가상매수 노출과
  한도 상태가 바로 보이지 않았음
- 텔레그램에서 시스템 상태를 판단해야 하므로 노출 가시성이 필요함

### 수정 사항
- `TelegramLiquidityLabController._build_virtual_exposure_lines()` 추가
- `/lab_portfolio`에 `─── 가상 노출 ───` 섹션 추가
  - 가상매수노출 금액
  - 종목 수
  - `max_virtual_exposure_pct` 기반 한도 문구
  - 최근 주문가능 USD 기준이 있으면 정상/초과 상태 표시
- 해외 주문가능 USD 조회 시 최근 기준값을 서비스에 저장
- 큰 금액의 가독성을 위해 가상매수노출/최근한도 표기를 `$393,294.92`처럼
  천 단위 구분과 소수 2자리 형식으로 변경

### 기대 효과
- 가상 포지션이 과도하게 쌓였는지 텔레그램 포트폴리오에서 즉시 확인 가능
- 신규 가상매수 제한이 왜 걸리는지 해석하기 쉬워짐

## [2026-07-10] 정규장 외 실보유 해외 매도 가상기록 차단

### 추가 점검 결과
- `MSEX`는 KIS 모의계좌에 실제 522주, 주문가능 522주로 남아 있었음
- 과거 정규장 외 매도 거부가 내부 `virtual sell`/정산대기 흐름으로 기록되어
  실제 보유와 가상 성과가 섞일 수 있는 경로가 확인됨

### 수정 사항
- 실보유 해외 종목 매도는 모의투자 정규장 외 세션에서 실제 주문을 보내지 않고
  `session_not_orderable_in_profile`로 보류
- 해당 보류는 `SELL` 상태로 저장하되 가상 매도 주문/정산대기를 만들지 않음
- 가상-only 보유 종목의 가상 청산 경로는 기존대로 유지

### 기대 효과
- 실제 KIS 잔고가 남아 있는데 가상 성과에서는 매도된 것처럼 보이는 혼선 방지
- 정규장 외 반복 매도 거부와 불필요한 가상 정산대기 생성 감소

## [2026-07-10] 가상 성과 오염 행 제외 플래그 추가

### 추가 점검 결과
- `/lab_portfolio`의 virtual 누적손익이 과거 버그성 기록까지 합산해
  현재 전략 성과 판단을 왜곡할 수 있었음
- 확인된 오염 행:
  - `MSEX` id=180: 실보유 매도 거부가 가상 매도 성과로 기록된 행
  - `PLBL` id=193: 비정상 급락 호가 guard 적용 전 `-35.86%`로 가상 청산된 행

### 수정 사항
- `virtual_orders`에 `excluded_from_performance`, `exclude_reason`, `excluded_at` 컬럼 추가
- virtual 성과 요약과 세션 성과 요약은 제외 플래그가 없는 sell만 집계
- 실제 DB는 `data/trading_backup_20260710_085920_pre_virtual_performance_exclusion.db`
  백업 후 id=180, id=193을 성과 제외 처리

### 기대 효과
- 원본 거래 기록은 보존하면서 현재 성과 지표만 오염 없이 확인 가능
- 향후 비정상 호가/정책 버그성 기록이 발생해도 삭제 없이 감사 가능한 방식으로 제외 가능

## [2026-07-10] 포트폴리오 명령 실시간 실보유 보강

### 추가 점검 결과
- `/lab_portfolio`는 기본적으로 마지막 lab report의 포지션을 사용하므로
  서비스 재시작 직후나 report 갱신 전에는 실보유가 비어 보일 수 있었음
- KIS 직접 조회에서는 국내 4종목과 해외 `MSEX` 522주가 확인됨

### 수정 사항
- `/lab_portfolio` 응답 직전에 KIS 국내/해외 잔고를 한 번 조회해 실보유 override 생성
- 실보유 섹션뿐 아니라 가상보유 합산 섹션도 같은 live 실보유 기준을 사용
- API 조회 실패 시에는 기존 last_report 기반 표시로 fallback

### 기대 효과
- 텔레그램 포트폴리오 조회 시 실제 계좌 보유와 표시 불일치 감소
- 재시작 직후에도 실보유 종목이 누락되어 보이는 현상 완화

## [2026-07-10] KIS 미국 주간거래 시간 보정

### 추가 점검 결과
- 공식 한국투자증권 거래시간 안내 기준 미국 주간거래는 KST 10:00~18:00,
  서머타임 10:00~17:00임
- 기존 `market_sessions.py`는 주간거래를 09:00~15:00/16:00으로 판단해
  09시대 장외 구간을 열린 세션으로 오판할 수 있었음

### 수정 사항
- `get_us_trading_session()`의 daytime 구간을 KIS 기준 10:00~17:00/18:00으로 수정
- 실전(`prod`)의 다음 미국 주문 가능 세션은 주간거래 10:00 기준,
  모의(`vps`)의 다음 주문 가능 세션은 정규장 22:30/23:30 기준으로 계산
- 09:30 KST 장외 판정, daytime 중 prod/vps 대기시간 차이를 회귀 테스트로 추가

### 기대 효과
- 09시대 비지원 구간에서 가상 매수/매도 또는 감시 루프가 열린 세션으로 오판하는 문제 감소
- README에 문서화된 거래시간 정책과 실제 코드 동작 일치

## [2026-07-10] 포트폴리오 가상보유 실시간 현재가 보강

### 추가 점검 결과
- `/lab_portfolio`의 가상보유 현재가는 watch target, 잔고 캐시, lab symbol state 순서로
  보강되지만, 감시 캐시가 오래되면 stale 가격으로 손익이 표시될 수 있었음
- 실제 KIS quote API로 현재 가상 해외 포지션 15개 모두 현재가 조회 가능함을 확인

### 수정 사항
- `/lab_portfolio` 실행 시 가상 해외 포지션 최대 25개까지 실시간 quote 조회
- live quote 가격은 watch/state 캐시보다 우선해 가상보유 손익 계산에 사용
- quote 조회 실패 종목은 기존 캐시 기반 표시로 fallback
- KIS 초당 호출 제한을 피하기 위해 quote 조회는 2개씩 묶어 약 1초 간격으로 처리
  - 실제 확인: 가상 해외 15개 현재가 조회 성공, 약 10.5초 소요

### 기대 효과
- 포트폴리오 조회 시 가상보유 손익이 오래된 감시 가격에 묶이는 현상 감소
- 많은 가상 포지션이 쌓인 상태에서도 현재 노출과 손익을 더 신뢰 가능하게 확인

## [2026-07-10] 국내 실보유 로딩 누락 방지

### 추가 점검 결과
- `_load_domestic_positions()`가 국내 스캔 결과(`domestic_ranked`)가 비어 있으면
  KIS 잔고 조회 전에 즉시 빈 리스트를 반환하고 있었음
- 이 경우 실제 국내 보유 종목이 계좌에 있어도 감시/청산 후보에서 빠질 수 있음
- 잔고 row의 현재가가 평균단가보다 낮은 경우에도 평균단가 fallback이 손실을 숨길 수 있어
  실제 손익 표시와 exit 판단이 왜곡될 위험이 있었음

### 수정 사항
- 국내 스캔 후보가 없어도 KIS 잔고를 직접 조회해 국내 실보유 포지션을 로드
- KIS 숫자 문자열(`1,184`, `5,030`)을 `parse_kis_number()`로 파싱해 수량/가격 오해석 방지
- quote 후보가 없을 때는 잔고 row의 현재가 필드(`prpr`, `stck_prpr`, `now_pric`, `last_price`)를
  우선 사용하고, 모두 없을 때만 평균단가로 fallback
- 실제 확인:
  - `002990` 52주, 현재 16,820원, 손익 -0.12%
  - `042660` 11주, 현재 81,300원, 손익 -1.09%
  - `058730` 184주, 현재 5,030원, 손익 -5.27%
  - `379800` 34주, 현재 25,600원, 손익 -0.66%

### 기대 효과
- 국내 동적 스캔 실패/공백 시에도 실제 보유 종목의 청산 감시가 유지됨
- 손실 중인 국내 보유 종목이 평균단가로 덮여 `0%`처럼 보이는 문제 감소

## [2026-07-10] 포트폴리오 조회용 KIS client 분리

### 추가 점검 결과
- 매매 사이클은 `async with KisRestClient(...)`로 KIS client를 열고 닫지만,
  `TelegramLiquidityLabController.lab_service`는 마지막 사이클의 닫힌 client를 들고 있을 수 있음
- 이 상태에서 `/lab_portfolio`가 기존 `lab_service.client`에 의존하면 실시간 실보유/현재가 조회가
  실패하고 오래된 report/state fallback으로 표시될 위험이 있음
- 서비스는 켜져 있어도 거래 루프가 `stopped`인 상황이 텔레그램 메시지에서 충분히 명확하지 않았음

### 수정 사항
- `/lab_portfolio` 실행 시 명령 처리 전용 임시 `KisRestClient`와 `LiquidityLabService`를 생성해
  실보유와 가상보유 현재가를 조회
- 같은 임시 client로 주문가능 USD를 1회 조회해 가상 노출의 최근한도/초과 여부를 표시
- 기존 `lab_service`의 동적 종목명/풀/최근 주문가능 USD 같은 표시용 상태는 임시 service로 복사
- 국내 live portfolio 파싱도 `parse_kis_number()`를 사용해 콤마가 포함된 수량/가격 문자열 처리 강화
- `/lab_status`, `/lab_portfolio`에 `거래루프=중지됨 (/lab_start 필요)` 같은 명확한 루프 상태 안내 추가
- 실제 확인: 가상매수노출 `$393,294.92`, 최근한도 `$171,292.39`, 상태 `초과` 표시

### 기대 효과
- 재시작 직후 또는 거래 루프 stopped 상태에서도 포트폴리오 실시간 조회 안정성 향상
- 사용자가 systemd 서비스 active 상태와 실제 거래 루프 stopped 상태를 혼동할 가능성 감소
- 가상 포지션이 주문가능 USD 대비 과도하게 쌓였는지 포트폴리오 조회만으로 즉시 판단 가능

## [2026-07-10] 주문 알림 체결 착시 완화

### 추가 점검 결과
- KIS 주문 응답의 `주문 완료`는 대체로 주문 접수/전송 완료이며 실제 체결 확정과 다를 수 있음
- 기존 텔레그램 큐 알림은 `매수`, `매도`로 표시되어 MTS의 미체결/부분체결 상태와 혼동될 수 있었음
- 특히 지정가 주문이 기본인 현재 구조에서는 주문 접수 후 체결까지 지연되거나 미체결로 남을 수 있음
- `/lab_log`의 실주문 손익도 `cycle_log`의 `SELL_REAL` 접수 기록 기반이므로 체결확정 손익으로
  오해될 수 있었음

### 수정 사항
- 실제 주문 알림 문구를 `매수접수`, `매도접수`로 변경
- 가상거래 알림 문구는 `가상매수`, `가상매도`로 변경
- `_send_summary()`의 `동작=` 필드도 동일한 표시 규칙을 사용하도록 통일
- `/lab_log`의 실주문 섹션 제목을 `실주문접수 기준`으로 변경하고
  `주의=체결확정은 MTS/잔고 기준 확인` 문구 추가

### 기대 효과
- 텔레그램 알림을 실제 체결 확정으로 오해하는 문제 감소
- 미체결 주문 정정/취소 흐름과 주문 접수 알림의 의미가 더 명확해짐
- 성과 요약에서 접수 기준 추정 손익과 실제 체결 손익을 구분하기 쉬워짐

## [2026-07-10] 감시데이터 신선도 표시 추가

### 추가 점검 결과
- 서비스 재시작 후에도 마지막 watchlist/report가 runtime state에 남아 있을 수 있음
- 거래 루프가 `stopped`인데 `/lab_status`에 과거 `다음실행`과 `다음간격`이 표시되어
  실제로 곧 실행될 것처럼 보일 수 있었음
- `/lab_watchlist`가 오래된 감시값을 최신 감시처럼 보이게 만들 수 있었음

### 수정 사항
- `/lab_status`, `/lab_watchlist`에 `감시데이터=46분 전 (루프 stopped)` 같은 신선도 표시 추가
- 최신 report가 없으면 `감시데이터=없음 (/lab_start 후 생성)`으로 표시
- 루프가 `running`이 아니면 status의 `다음실행`, `다음간격`을 `-`로 표시

### 기대 효과
- 서비스 active 상태와 실제 거래 루프/감시 데이터 상태를 명확히 구분
- 오래된 watchlist를 현재 실시간 감시 결과로 오해하는 문제 감소

## [2026-07-10] `/lab_orders` 주문기록 조회 명령 추가

### 추가 점검 결과
- `broker_order_events`에는 실주문/가상주문/취소 이벤트가 저장되고 있지만 텔레그램에서
  최근 주문 접수 이력을 직접 조회할 명령이 없었음
- 최근 실주문 165건이 `SUBMITTED` 상태로 남아 있어, 체결확정과 주문접수 이력을 구분해
  확인할 수 있는 운영 화면이 필요했음

### 수정 사항
- `/lab_orders` 명령 추가
- 최근 `broker_order_events`를 `매수접수`, `매도접수`, `취소`, `가상매수기록`,
  `가상매도기록` 형식으로 표시
- 메시지 상단에 `기준=주문 접수/취소/가상기록 (체결확정 아님)` 문구 추가
- 실제 확인: 국내 360750/233740/379800 등 최근 주문번호와 접수 가격이 텔레그램 형식으로 표시됨

### 기대 효과
- MTS에서 보이는 미체결/체결 상태와 봇 내부 주문 접수 기록을 대조하기 쉬워짐
- 주문거부, 미체결 정정, 취소 후 재주문 흐름을 추적하는 기본 운영 도구 확보

## [2026-07-10] `/lab_orders` live 해외 미체결 섹션 추가

### 추가 점검 결과
- `/lab_orders`가 내부 주문 이벤트는 보여주지만, 현재 KIS 서버에 남아 있는 해외 미체결 주문은
  직접 표시하지 않았음
- 과거 ALNY/BBIO/PCAP 이슈처럼 미체결 주문이 주문가능수량을 막는 상황에서는 live 미체결 여부가
  가장 먼저 확인되어야 함

### 수정 사항
- `/lab_orders` 실행 시 KIS 해외 주문 체결내역 API를 조회해 `live 해외 미체결` 섹션 표시
- 모의투자(`vps`)는 전체 해외 미체결을 1회 조회하고, 실전(`prod`)은 최근 해외 주문 심볼만 제한 조회
- 미체결 주문은 `매수미체결`/`매도미체결`, 가격, 수량, 주문번호 형식으로 표시
- 실제 확인: 현재 해외 live 미체결 `0건`, `/lab_orders`에 `미체결=없음` 표시

### 기대 효과
- 기존 주문 때문에 매도/매수 가능수량이 막히는 상황을 텔레그램에서 빠르게 확인 가능
- 내부 접수 기록과 KIS 서버의 현재 미체결 상태를 한 화면에서 비교 가능

## [2026-07-10] `/lab_orders` live 국내 미체결 섹션 추가

### 추가 점검 결과
- `/lab_orders`가 해외 live 미체결은 표시하지만 국내 미체결은 내부 `broker_order_events`
  접수 기록만 보여주고 있었음
- 운영 DB에는 국내 주문 접수 이벤트가 다수 남아 있어, 실제 KIS 서버에 미체결 주문이 남아 있는지
  텔레그램에서 바로 확인할 필요가 있었음
- KIS 공식 샘플 기준 국내 주문체결조회는
  `/uapi/domestic-stock/v1/trading/inquire-daily-ccld`와 `TTTC0081R/VTTC0081R`를 사용
  (`TTTC8001R/VTTC8001R`는 폴백으로 유지)
- 실제 확인: 현재 국내 live 미체결 1건
  `073240 금호타이어 매수 126주 @ 6,990원 주문번호=0000013669`

### 수정 사항
- `KisRestClient.get_domestic_order_history()` 추가
  - 최신 TR ID 실패 시 구 TR ID로 폴백
  - `output1`, `output`, `output2`, 연속조회 키를 통합 반환
- `/lab_orders`에 `live 국내 미체결` 섹션 추가
  - 당일 KIS 국내 주문체결조회에서 `CCLD_DVSN=02` 미체결 주문 조회
  - `rmn_qty`가 없으면 `ord_qty - tot_ccld_qty - cncl_cfrm_qty - rjct_qty`로 잔여수량 계산
  - 종목코드/종목명, 매수·매도미체결, 가격, 수량, 주문번호 표시
- 국내/해외 live 미체결 라인에 `경과=` 표시 추가
  - 30분 이상 남아 있는 주문은 `주의=장기미체결`로 표시
- 주문 사유 표시에서 `domestic_buy`, `strategy_buy_signal`, `stale_exit_replace`를 한국어로 매핑

### 기대 효과
- 국내 미체결 주문이 주문가능수량을 막거나 오래 남아 있는 상황을 텔레그램에서 즉시 파악 가능
- 내부 접수 기록과 KIS 서버 live 미체결 상태를 국내/해외 모두 같은 화면에서 비교 가능
- 오래된 미체결 주문이 장시간 방치되는 문제를 사용자가 `/lab_orders`에서 즉시 인지 가능
- 사용자 입장에서 `/lab_orders`가 “주문 접수 기록 + 실제 미체결 확인” 역할을 더 명확히 수행

## [2026-07-10] 국내 장기미체결 텔레그램 취소 명령 추가

### 추가 점검 결과
- `/lab_orders` live 조회 결과 국내 073240 금호타이어 매수 미체결 주문이 9시간 이상 남아 있었음
- 국내 미체결 주문도 해외와 마찬가지로 주문가능수량/예수금/다음 전략 판단을 흐릴 수 있음
- KIS 국내 정정취소 API:
  - endpoint: `/uapi/domestic-stock/v1/trading/order-rvsecncl`
  - 최신 TR ID: `TTTC0013U/VTTC0013U`
  - 구 샘플 TR ID: `TTTC0803U/VTTC0803U`
  - 잔량 전체 취소 시 `QTY_ALL_ORD_YN=Y`, `ORD_QTY=0`, `ORD_UNPR=0`

### 수정 사항
- `KisRestClient.revise_or_cancel_domestic_order()` 추가
  - 최신 TR ID 실패 시 구 TR ID로 폴백
  - 잔량 전체 취소 요청이면 수량과 취소 단가를 0으로 보정
- 텔레그램 명령 추가
  - `/lab_cancel_stale_domestic`: 30분 이상 국내 미체결 주문 미리보기
  - `/lab_cancel_stale_domestic_confirm`: 미리보기 대상 국내 미체결 주문을 KIS에 취소 요청
- 실제 취소 요청 결과는 `broker_order_events`에 `order_kind=cancel`,
  `reason=stale_live_order_cancel`, `status=CANCELED`로 기록

### 안전 정책
- 자동 취소는 하지 않음
- 사용자가 `/lab_cancel_stale_domestic`으로 대상 확인 후
  `/lab_cancel_stale_domestic_confirm`을 직접 보내야 실제 취소 요청 진행
- confirm 명령은 실수 방지를 위해 봇 메뉴에는 노출하지 않고 prompt에서만 안내

## [2026-07-10] 사이클 취소를 최근오류로 남기지 않도록 수정

### 추가 점검 결과
- 서비스 배포/재시작 또는 `/lab_stop` 과정에서 실행 중이던 사이클이 `CancelledError`로 종료될 수 있음
- 기존에는 이 상황이 `cycle_N_cancelled` 형태로 `last_error`에 남아 `/lab_status`에서 실제 장애처럼 보일 수 있었음
- 현재 runtime state에도 `cycle_1149_cancelled`가 남아 있었으나, 이는 배포/중지 과정의 흔적으로 판단됨

### 수정 사항
- `_run_cycle()`의 `asyncio.CancelledError`는 오류로 저장하지 않고 `last_error=None` 처리
- `_drain_finished_cycle()`, `_handle_stop()`, `_handle_terminate()`의 취소 경로도 동일하게 정리
- `_restore_runtime_state()`에서 과거 `cycle_*_cancelled` 오류 문자열은 복원하지 않도록 필터링

### 기대 효과
- 의도된 중지/재시작과 실제 런타임 오류를 구분하기 쉬워짐
- `/lab_status`의 `최근오류`가 배포 흔적으로 오염되는 문제 감소

## [2026-07-10] 최근 3일 성과 기반 해외 리스크 축소

### 추가 점검 결과
- `scripts/analyze_trades.py data/trading.db --days 3` 기준:
  - 국내 실주문접수: 51건, 평균 +0.471%, net +104,561원
  - 해외 실주문접수: 39건, 평균 +0.169%이나 net -2,333,025원
  - 해외 VWAP 단독: 24건, net -2,908,982원으로 손실 기여가 큼
  - 가상거래 stop_loss: 15건, 누적 -8,061.38 USD로 손실 대부분을 차지
- 해외 가상/실주문 모두 큰 손실이 일부 종목에서 크게 발생하는 구조로 보여
  당장 진입 빈도보다 손실 확산 방지가 우선이라고 판단

### 수정 사항
- `config/fixed_config.json`
  - `max_concurrent_overseas_orders`: 20 → 8
  - `overseas_stop_loss_pct`: 0.015 → 0.010

### 기대 효과
- 해외 신규 진입의 동시 노출을 줄여 가상/실계좌 모두 과도한 포지션 누적 완화
- 해외 손절이 더 빠르게 작동해 stop_loss 1건당 손실 폭 축소
- 국내 쪽 상대 우수 성과는 유지하면서 해외 리스크를 보수적으로 낮춤

## [2026-07-10] 거래 분석 스크립트 보강

### 수정 사항
- `scripts/analyze_trades.py`
  - `cycle_log.net_pnl_krw/net_pnl_usd`가 있으면 net 기준으로 실주문접수 손익 집계
  - 출력 상단에 “실주문 통계는 주문 접수 기준, 체결확정은 MTS/잔고 확인 필요” 문구 추가
  - `virtual_orders.excluded_from_performance=1` 항목은 가상거래 성과 집계에서 제외
  - 전략별 실주문접수 손익 섹션 추가
  - 가상거래 청산 이유별 손익 섹션 추가

### 기대 효과
- 전략 조정 시 gross/old virtual 데이터에 속는 문제 감소
- 국내/해외, 전략, 청산 사유별로 손실 원인을 더 빠르게 식별 가능

## [2026-07-10] 주문 거부 관측성 보강

### 추가 점검 결과
- 최근 24시간 `cycle_log` 기준:
  - `sell:no_orderable_qty` 29건
  - `sell:order_rejected` 21건
- 해외 `ALNY`의 반복 거부는 이전 운용 코드/미체결 주문 영향으로 보이며,
  현재 코드는 해외 `order_rejected` 시 20분 쿨다운을 적용 중
- 국내 `069500`, `379800`에서도 1회성 `sell:order_rejected` 흔적이 있어,
  국내 매도 거부 쿨다운이 회귀하지 않도록 테스트 보강 필요

### 수정 사항
- `tests/test_liquidity_lab.py`
  - 국내 매도 주문 거부 시 10분 쿨다운이 등록되고,
    `cycle_log.exit_cooldown_remaining`에도 9분 이상으로 기록되는지 검증 추가
- `scripts/analyze_trades.py`
  - 전체 분석(`--days 0`) 경로의 f-string SQL 누락 수정
  - 전략별 손익 집계에서 `exit_by`가 비어 있으면 `action_reason`을 청산 원인으로 표시

### 기대 효과
- 국내/해외 모두 주문 거부 후 즉시 반복 재시도되는 문제의 회귀 방지
- 전략별 분석에서 `exit=N/A`로 뭉개지던 항목을
  `trend_filter_lost`, `stop_loss`, `momentum_loss_cut` 등 실제 청산 정책별로 파악 가능

## [2026-07-10] 국내 장기 미체결 취소 실패 원인 기록

### 확인 결과
- KIS 실시간 미체결 조회 기준:
  - 국내 `073240` 금호타이어 매수미체결 126주 @ 6,990원
  - 경과 약 9시간 30분 이상
  - 해외 미체결 없음
- 취소 요청 직접 확인 결과:
  - `VTTC0803U error: 40580000 모의투자 장종료 입니다.`
- 결론: 주문번호/기관번호 문제는 아니며,
  KIS 모의투자 국내 취소 API가 장종료 후 취소 요청을 거부하는 상태

### 수정 사항
- `telegram_control.py`
  - 장기 국내 미체결 취소 실패도 `broker_order_events`에
    `status=REJECTED`, `reason=stale_live_order_cancel_failed`로 저장
  - 장종료 오류는 텔레그램에 `장종료(국내장중 재시도 필요)`로 명확히 표시
  - `/lab_orders`의 국내 장기 미체결 라인에 장외 시간에는
    `취소가능=국내장중` 안내를 추가
  - 텔레그램 서비스 유지보수 루프에서 국내 정규장 중 10분 간격으로
    봇이 접수한 장기 미체결 국내 주문을 자동 취소 시도
- `message_format.py`
  - `stale_live_order_cancel_failed` → `장기미체결 취소거부` 사유 매핑 추가
  - `/lab_orders`에서 취소 실패 이벤트가 `매수접수`로 오인되지 않도록
    `취소거부` 액션 표시 추가

### 운영 메모
- 현재 남아 있는 `073240` 미체결 매수는 다음 국내 정규장에
  자동 취소 루틴이 우선 재시도하며, 수동으로는
  `/lab_cancel_stale_domestic_confirm`를 사용할 수 있음

## [2026-07-10] RSI 단독 진입 추가 보수화

### 근거
- 최근 1일 실주문접수 분석:
  - 해외 RSI 단독: 4건, 승률 0%, net -2,296,026원
  - 국내 RSI 단독: 4건, net -20,139원
  - 국내 VWAP+RSI 복합: 3건, 승률 100%, net +44,769원
- RSI가 다른 전략과 결합될 때는 긍정적이지만,
  RSI 단독 진입은 해외 손실 기여가 커서 더 강한 과매도 조건 필요

### 수정 사항
- `config/fixed_config.json`
  - `rsi_entry_threshold`: 35.0 → 30.0

### 기대 효과
- RSI 단독 진입은 RSI 30 이하에서만 허용
- 성과가 좋았던 VWAP+RSI 복합 진입은 기존처럼 유지
- 해외 RSI 단독 손실 빈도 감소 기대

## [2026-07-10] README 텔레그램 명령/기본값 동기화

### 수정 사항
- `README.md`
  - `max_concurrent_overseas_orders` 기본값 20 → 8로 갱신
  - 제거된 `/lab_positions`, `/lab_virtual` 설명을 `/lab_portfolio`로 대체
  - `/lab_orders`, `/lab_cancel_stale_domestic`, `/lab_cancel_stale_domestic_confirm` 설명 추가
  - 국내 장기 미체결은 장외 시간에 `취소가능=국내장중`으로 표시되고,
    봇 접수 주문은 다음 국내 정규장에 자동 취소 재시도됨을 문서화

### 기대 효과
- 실제 텔레그램 명령과 README가 어긋나 사용자가 없는 명령을 호출하는 문제 감소

## [2026-07-10] 가상매수 노출 한도 축소

### 확인 결과
- `/lab_portfolio` 경로로 현재 상태를 로컬 확인:
  - 해외 가상매수노출 약 `$393,294.92`
  - 최근 주문가능 USD 기준 한도 약 `$171,292.39`
  - 상태=`초과`
- 가상거래 누적 성과는 최근 기준 음수이며, 해외 virtual stop_loss 손실 비중이 큼

### 수정 사항
- `config/fixed_config.json`
  - `max_virtual_exposure_pct`: 1.0 → 0.5

### 기대 효과
- KIS 모의투자 거래불가 세션에서 virtual 포지션이 과도하게 쌓이는 문제 완화
- 기존 초과 노출이 청산되기 전까지 신규 가상매수 재개를 더 강하게 제한

## [2026-07-10] 포트폴리오 실보유 리스크 경고 추가

### 확인 결과
- 현재 `/lab_portfolio` 경로 기준:
  - 거래루프는 `stopped`
  - 국내 `058730` 실보유 손익 약 -5.27%
- 거래 루프가 중지되어 있으면 자동 손절/청산 감시가 동작하지 않으므로
  포트폴리오 메시지에서 더 명확한 주의 표시가 필요

### 수정 사항
- `telegram_control.py`
  - `/lab_portfolio` 메시지에 `실보유 리스크` 섹션 추가
  - 국내는 `auto_trade.hard_stop_loss_pct`, 해외은
    `liquidity_lab.overseas_stop_loss_pct`를 기준으로 손실 경고 표시
  - 루프 중지 상태에서 기준 이하 손실 포지션이 있으면
    `자동 청산 감시가 동작하지 않습니다` 경고 표시

### 기대 효과
- 사용자가 `/lab_portfolio`를 볼 때 단순 손익 숫자뿐 아니라
  자동감시 중지로 인한 방치 리스크를 즉시 인지 가능

## [2026-07-10] 해외 장기 미체결 자동 취소 루틴 추가

### 배경
- 국내 장기 미체결은 자동 취소 루틴이 추가됐지만,
  해외 미체결은 매도/매수 경로 내 정리와 수동 확인 중심이라
  서비스가 대기 중인 경우 장기 미체결이 남을 가능성이 있었다.
- 기존 `both waiting`/`no_orderable_qty` 문제는 미체결 주문이
  다음 주문 가능 수량을 잠그는 구조였으므로 해외도 같은 방어선 필요.

### 수정 사항
- `telegram_control.py`
  - 미국 주문 가능 세션에서 10분 간격으로 해외 장기 미체결 자동 점검
  - 봇이 `SUBMITTED`로 기록한 해외 주문번호만 자동 취소 대상에 포함
  - 취소 성공 시 `stale_live_overseas_order_cancel`
  - 취소 실패 시 `stale_live_overseas_order_cancel_failed`로 주문 이벤트 기록
- `message_format.py`
  - 해외 장기미체결 취소/취소거부 한글 사유 매핑 추가

### 기대 효과
- 해외 미체결 주문이 장시간 남아 이후 매도/매수 주문을 막는 상황 감소
- 사용자 수동 주문은 자동 취소 대상에서 제외해 운영 안전성 유지

## [2026-07-10] 휴장 판단 기준일 명시화

### 배경
- 텔레그램 상태/자동 미체결 취소/랩 휴장 override가 대부분 실시간 기준으로 동작하지만,
  일부 휴장 함수 호출이 명시 기준일 없이 내부의 현재일을 다시 조회하고 있었다.
- UTC, KST, 뉴욕 시간이 서로 날짜 경계에 걸릴 때 테스트 기준 시각과
  휴장 판단 기준일이 엇갈릴 수 있는 잔여 리스크가 있었다.

### 수정 사항
- `telegram_control.py`
  - 상태 메시지, 국내/해외 자동 장기 미체결 취소에서
    각각 KST/뉴욕 기준 날짜를 명시적으로 전달
- `liquidity_lab.py`
  - 휴장 override 판단과 휴장 알림 본문이 동일한 기준일을 사용하도록 수정
- `market_calendar.py`
  - `market_status_summary()`가 선택적으로 KRX/NYSE 기준일을 받을 수 있게 확장

### 검증
- 기준일 전달 회귀 테스트 추가
- `python3 -m pytest tests -q` → 361개 통과

## [2026-07-10] cycle_log 감시/SKIP 로그 비거래 분리

### 배경
- `cycle_log.is_session_trade`는 실제 세션 매매 성과 분석에서 쓰이는 구분값이지만,
  감시 대상 HOLD/WAIT/BUY 신호 로그와 SKIP 로그도 기본값 1로 저장될 수 있었다.
- 현재 손익 집계는 `SELL_REAL` 중심이라 즉시 손익 계산 오류는 제한적이지만,
  이후 전략별 분석 쿼리에서 감시 로그를 실제 거래 로그로 오해할 여지가 있었다.

### 수정 사항
- `liquidity_lab.py`
  - `_save_cycle_log_from_watch_target()` 감시 로그는 `is_session_trade=0`
  - `_record_trade_skip()` 주문 미진행/SKIP 로그도 `is_session_trade=0`
- 테스트
  - 가격 급변 확인 SKIP, no_orderable SKIP, watch target cycle log가
    모두 비거래 로그로 저장되는지 검증 추가

### 검증
- `python3 -m pytest tests -q` → 361개 통과

## [2026-07-10] 해외 VWAP 단독 진입 차단

### 성과 근거
- `cycle_log` 기준 누적 실매도 148건을 재집계:
  - 해외 전체: 82건, 순손익 약 -2,208,142원
  - 국내 전체: 66건, 순손익 약 +197,652원
  - 해외 `VWAP` 단독: 41건, 순손익 약 -3,052,452원
  - 해외 `VWAP+RSI`: 7건, 순손익 약 +362,494원
  - 해외 `VOL`: 6건, 순손익 약 +814,640원
- 손실은 해외 VWAP 단독의 `trend_filter_lost`가 가장 크게 누적됨.

### 수정 사항
- `config/fixed_config.json`
  - `liquidity_lab.overseas_block_standalone_vwap=true` 추가
- `config.py`
  - `LiquidityLabConfig.overseas_block_standalone_vwap` 로딩 추가
- `liquidity_lab.py`
  - 해외 전략 신호가 정확히 `VWAP` 단독이면 신규 매수를 `WAIT` 처리
  - `VWAP+RSI`, `VWAP+VOL`, `VOL` 등 복합/거래량 신호는 기존처럼 허용
- `README.md`
  - 해외 VWAP 단독 차단 정책과 `overseas_scan_top_n=25` 기본값 문서화

### 검증
- 해외 VWAP 단독 차단/복합 신호 허용 테스트 추가
- `python3 -m pytest tests -q` → 363개 통과

## [2026-07-10] 해외 장기 미체결 수동 취소 명령 추가

### 배경
- 해외 장기 미체결은 자동 취소 루틴이 있지만,
  사용자가 텔레그램에서 즉시 확인/확정 취소할 수 있는 명령은 국내만 있었다.
- `both waiting`, `no_orderable_qty`류 주문 꼬임이 다시 발생할 때
  자동 주기까지 기다리지 않고 직접 정리할 수 있는 운영 수단이 필요했다.

### 수정 사항
- `telegram_control.py`
  - `/lab_cancel_stale_overseas` 명령 추가
  - `/lab_cancel_stale_overseas_confirm` 확정 취소 명령 추가
  - 해외 confirm 조회 실패/대상 없음도 텔레그램 메시지로 응답
- `README.md`
  - 해외 장기 미체결 확인/확정 취소 명령 문서화
- 테스트
  - 명령 파서, 해외 prompt, 해외 취소 실행 경로 검증

### 검증
- `python3 -m pytest tests -q` → 364개 통과

## [2026-07-10] 휴장일 기반 다음 세션 계산 보강

### 배경
- `minutes_until_next_tradeable_session()`과 `determine_loop_interval_sec()`가
  평일/시간대만 기준으로 다음 거래 가능 세션을 계산하고 있었다.
- KRX/NYSE 휴장일의 장 시작 직전에는 실제 거래가 없는데도
  감시 주기가 30초로 짧아질 수 있는 잔여 리스크가 있었다.
- 미국장은 KIS가 한국시간 기준으로 데이타임/프리/정규/애프터 세션을
  제공하므로, 단순 뉴욕 현재 날짜만 보면 주간거래 날짜가 어긋날 수 있었다.

### 수정 사항
- `market_sessions.py`
  - `us_holiday_date_for_kis_session()` 추가
  - KRX/NYSE 휴장일은 다음 거래 가능 세션 후보에서 제외
  - 휴장일 프리마켓/애프터마켓에서는 감시 주기를 30초로 당기지 않음
- `telegram_control.py`, `liquidity_lab.py`
  - 미국 휴장 판단 날짜를 KIS 세션 기준 날짜로 통일
- `README.md`
  - 휴장일을 다음 세션 계산에서 건너뛰는 정책 문서화

### 검증
- KRX 휴장, NYSE 휴장, KIS 세션 기준 날짜 회귀 테스트 추가
- `python3 -m pytest tests -q` → 369개 통과

## [2026-07-10] README 기본 주기/스캔 수 동기화

### 수정 사항
- README에 남아 있던 오래된 기본값을 현재 `config/fixed_config.json`과 일치시킴
  - `poll_interval_sec`: 10초 표기 → 25초
  - `overseas_scan_top_n`: 12 표기 → 25

### 기대 효과
- 실제 운영 주기와 문서 설명이 달라 사용자가 감시 빈도/스캔 범위를 오해하는 문제 감소

## [2026-07-10] 기존 cycle_log 비거래 플래그 보정

### 배경
- 신규 감시/HOLD/WAIT/SKIP 로그는 `is_session_trade=0`으로 저장되도록 수정했지만,
  과거 DB에는 `HOLD`, `WAIT`, `BUY`, `SELL`, `SKIP` 감시 로그가
  `is_session_trade=1`로 남아 있었다.
- 누적 전략 분석에서 감시 신호를 실제 거래처럼 오해할 수 있어
  기존 데이터도 동일 기준으로 보정할 필요가 있었다.

### 수정 사항
- `repository.py`
  - 초기화 시 `cycle_log.action_bias NOT IN ('BUY_REAL', 'SELL_REAL')`인 기존 행의
    `is_session_trade`를 0으로 보정
- `tests/test_repository.py`
  - 기존 비거래 로그 플래그가 재초기화 시 0으로 보정되고,
    `BUY_REAL`/`SELL_REAL`은 유지되는지 검증
- 실제 `data/trading.db` 확인:
  - `SKIP/HOLD/WAIT/BUY/SELL`의 `session_trade_1` → 0
  - `BUY_REAL`은 169건 유지
  - `SELL_REAL`은 기존 세션 소유 판정대로 89건 유지

### 검증
- `python3 -m pytest tests -q` → 370개 통과

## [2026-07-10] 포트폴리오 가상보유 리스크 표시 보강

### 배경
- `/lab_portfolio`는 실보유 종목의 손절 기준 초과 위험은 별도 섹션으로 표시했지만,
  가상보유 종목은 손절 기준을 넘거나 총 가상 노출이 한도를 초과해도
  일반 보유/노출 줄만 보고 판단해야 했다.
- 거래 루프가 중지된 상태에서는 가상 포지션의 자동 청산 감시도 멈추므로,
  사용자가 포트폴리오 조회만으로 위험 상태를 바로 알아야 한다.

### 수정 사항
- `telegram_control.py`
  - 가상보유 종목도 현재가와 평균단가 기준 손익이 손절 기준을 넘으면
    `가상보유 리스크` 섹션에 표시
  - 거래 루프가 중지된 경우 `상태=감시중지`와 자동 청산 감시 중지 경고 표시
  - 가상매수 노출이 주문가능 USD 기준 한도를 초과하고 루프가 중지된 경우
    `상태=초과 감시=중지` 및 별도 주의 문구 표시
- `tests/test_telegram_control.py`
  - 가상 손절 리스크와 가상 노출 초과 경고가 동시에 표시되는 회귀 테스트 추가

### 검증
- `python3 -m pytest tests/test_telegram_control.py::test_build_portfolio_message_uses_available_usd_override_for_virtual_exposure tests/test_telegram_control.py::test_build_portfolio_message_warns_virtual_risk_and_exposure_when_stopped -q` → 2개 통과
- `python3 -m pytest tests -q` → 371개 통과

## [2026-07-10] 주문 거부 원문 로깅 보강

### 배경
- 최근 국내 `sell:order_rejected` 이벤트는 남아 있었지만,
  실제 KIS 거부 메시지가 `event_log.detail`에 남지 않아
  장종료/미체결/잔고/주문 대기 중 어느 원인인지 사후 분석이 어려웠다.
- 해외 `Both-sided waiting order exists`처럼 원문이 있어야 바로 판단되는
  주문 오류도 동일한 관측성 보강이 필요했다.

### 수정 사항
- `liquidity_lab.py`
  - `_record_trade_skip()`에 선택적 `error` 필드 추가
  - 국내 매수/매도 주문 거부 시 `event_log.detail.error`에 KIS 원문 저장
  - 해외 매수/매도 주문 거부 및 `no_orderable_qty`성 API 거부 시 원문 저장
  - 실제 주문 요청이 KIS에서 거부된 경우 `broker_order_events`에
    `status=REJECTED`, `payload.error`를 남기도록 보강
- `tests/test_liquidity_lab.py`
  - 국내/해외 매도 거부 시 이벤트 로그와 broker event에 오류 원문이 남는지 검증

### 검증
- `python3 -m pytest tests/test_liquidity_lab.py::test_place_overseas_sell_order_rejected_adds_20min_cooldown tests/test_liquidity_lab.py::test_place_domestic_sell_order_rejected_adds_10min_cooldown_and_logs_it tests/test_liquidity_lab.py::test_domestic_sell_rejected_adds_10min_cooldown tests/test_liquidity_lab.py::test_domestic_buy_rejected_marks_skipped_true -q` → 4개 통과
- `python3 -m pytest tests -q` → 371개 통과

## [2026-07-10] watchlist 신호 캐시 상태 표시

### 배경
- 해외 보유/가상보유 종목 중 `stale_signal_cache` 기반으로 상태가 유지되는
  종목이 다수 보였다.
- 전체 감시 데이터 나이는 status/watchlist 상단에 표시되지만,
  개별 종목 라인에서는 해당 종목이 최신 신호가 아닌 캐시 기반인지 구분하기 어려웠다.

### 수정 사항
- `telegram_control.py`
  - watchlist 개별 라인의 `note`에 `stale_signal_cache`가 포함되면
    `신호=캐시`를 짧게 추가
- `tests/test_telegram_control.py`
  - stale signal cache 라벨 표시 테스트 추가

### 검증
- `python3 -m pytest tests/test_telegram_control.py::test_format_watch_target_line_is_compact tests/test_telegram_control.py::test_format_watch_target_line_ready_status_is_readable tests/test_telegram_control.py::test_format_watch_target_line_marks_stale_signal_cache -q` → 3개 통과
- `python3 -m pytest tests -q` → 372개 통과

## [2026-07-10] 주문기록 거부 오류 표시 보강

### 배경
- 주문 거부 원문은 `broker_order_events.payload.error`에 저장되도록 보강했지만,
  `/lab_orders` 출력에서는 상태와 사유만 보여 실제 거부 원문을 바로 볼 수 없었다.

### 수정 사항
- `telegram_control.py`
  - 내부 주문 이벤트가 `status=REJECTED`이고 `payload.error`가 있으면
    `/lab_orders` 한 줄에 `오류=...`를 추가
- `tests/test_telegram_control.py`
  - 거부된 국내 취소 주문에 오류 원문이 표시되는지 검증

### 검증
- `python3 -m pytest tests/test_telegram_control.py::test_build_recent_order_events_message_formats_submission_cancel_and_virtual -q` → 1개 통과
- `python3 -m pytest tests -q` → 372개 통과

## [2026-07-10] 국내 미체결 취소 장외 실행 차단

### 배경
- 최근 `broker_order_events`에 국내 미체결 취소가 장 종료 후 실행되어
  `모의투자 장종료` 거부로 기록된 사례가 있었다.
- 자동 취소는 국내 정규장 중에만 실행되지만, 수동 확정 명령은 장외에도
  KIS 취소 요청을 보내 불필요한 거부 이벤트를 만들 수 있었다.

### 수정 사항
- `telegram_control.py`
  - `_execute_cancel_stale_domestic_orders()`에 현재시각 인자를 추가
  - 국내 정규장이 아니거나 KRX 휴장일이면 실제 KIS 취소 요청을 보내지 않고
    `상태=장외취소보류` 안내 후 `maintenance_skip` 이벤트만 저장
  - 자동 취소 경로는 이미 계산한 현재시각을 실행 함수에 전달
- `tests/test_telegram_control.py`
  - 장중 성공/거부 경로는 명시적 장중 시각으로 검증
  - 장외 실행 시 KIS 클라이언트를 열지 않고 broker event도 남기지 않는지 검증

### 검증
- `python3 -m pytest tests/test_telegram_control.py::test_execute_cancel_stale_domestic_orders_records_cancel_event tests/test_telegram_control.py::test_execute_cancel_stale_domestic_orders_records_rejected_event tests/test_telegram_control.py::test_execute_cancel_stale_domestic_orders_defers_when_market_closed -q` → 3개 통과
- `python3 -m pytest tests/test_telegram_control.py::test_maybe_auto_cancel_stale_domestic_orders_only_bot_submitted_orders tests/test_telegram_control.py::test_execute_cancel_stale_domestic_orders_defers_when_market_closed -q` → 2개 통과
- `python3 -m pytest tests -q` → 373개 통과

## [2026-07-10] 국내 보호청산 미체결 재주문 보강

### 발견 사고
- `058730(다스코)`:
  - 2026-07-09 09:20 KST 매수 184주 @ 5,310원
  - 10:08 KST 5,710원 매도 주문 접수(`partial_profit_lock`) 후 실제 체결되지 않음
  - 이후 보유수량 184주, 주문가능수량 0으로 반복되어 손절/ATR 청산 신호가
    `sell:no_orderable_qty`로 스킵됨
  - 2026-07-10 잔고 조회 결과 실제 모의계좌에 184주가 여전히 남아 있고,
    주문가능수량은 184주로 풀려 있음

### 원인
- 해외 매도 경로는 오래된 미체결 매도를 취소 후 재주문하는 정책이 있었지만,
  국내 매도 경로에는 동일한 보호청산 재주문 로직이 없었다.
- 국내 SELL 후보 선정도 `orderable_qty > 0` 조건 때문에,
  미체결 매도 주문에 수량이 묶인 손절 후보가 매도 함수까지 도달하지 못했다.

### 수정 사항
- `liquidity_lab.py`
  - 국내 미체결 주문 조회/파싱/취소 헬퍼 추가
  - 국내 SELL 후보 선정에서 `orderable_qty > 0` 대신 보유수량 기준으로 후보 유지
  - 보호청산(`stop_loss`, `atr_hard_stop`, `momentum_loss_cut`,
    `trend_filter_lost`, `time_exit_loss`)이고 기존 매도 미체결이 오래됐으면
    취소 후 현재 매도호가 기준으로 재주문
  - 보호청산이 아니거나 미체결 주문이 너무 최근이면 `pending_exit_order`로 중복 주문 방지
- `tests/test_liquidity_lab.py`
  - `058730`과 같은 `orderable_qty=0 + 오래된 매도 미체결 + atr_hard_stop`
    상황에서 취소 후 재매도 주문이 발생하는 회귀 테스트 추가

### 검증
- `python3 -m pytest tests/test_liquidity_lab.py::test_place_domestic_protective_sell_replaces_stale_pending_exit_when_orderable_zero tests/test_liquidity_lab.py::test_place_domestic_sell_order_sends_telegram_on_success tests/test_liquidity_lab.py::test_domestic_sell_rejected_adds_10min_cooldown -q` → 3개 통과
- `python3 -m pytest tests/test_liquidity_lab.py::test_select_domestic_exit_target_keeps_zero_orderable_positions_for_pending_repair tests/test_liquidity_lab.py::test_place_domestic_protective_sell_replaces_stale_pending_exit_when_orderable_zero -q` → 2개 통과
- `python3 -m pytest tests -q` → 374개 통과

## [2026-07-10] 해외 가능금액 과대평가 방지

### 발견
- KIS 해외 주문가능 조회에서 `MSEX` 기준 원문은 `ord_psbl_frcr_amt=67071.63`,
  `max_ord_psbl_qty=1217`인데 `frcr_ord_psbl_amt1=171292.388229`도 함께 내려왔다.
- 기존 `_get_overseas_available_usd()`는 여러 필드의 최댓값을 사용해,
  실제 주문가능 수량보다 큰 이론/환전 관련 금액이 슬롯 매수 수량과
  가상 노출 한도를 키울 수 있었다.
- 실제 새 계산 검증 결과: `1217 * 54.53 = 66363.01`달러로 보수적 캡 적용.

### 수정 사항
- `liquidity_lab.py`
  - 해외 가능금액은 실제 주문가능 외화금액 필드(`ord_psbl_frcr_amt`,
    `ovrs_ord_psbl_amt`, `echm_af_ord_psbl_amt` 등)를 우선 사용
  - `max_ord_psbl_qty`, `ord_psbl_qty`, `echm_af_ord_psbl_qty`가 있으면
    `수량 * 현재가`로 최종 상한 적용
  - `frcr_ord_psbl_amt1` 같은 큰 이론 금액은 직접 필드가 없을 때만
    fallback으로 사용

### 검증
- `python3 -m pytest tests/test_liquidity_lab.py::test_overseas_buy_uses_slot_sizing_when_balance_is_available tests/test_liquidity_lab.py::test_get_overseas_available_usd_caps_large_theoretical_amount_by_orderable_qty tests/test_liquidity_lab.py::test_virtual_overseas_buy_uses_slot_sizing_when_balance_is_available -q` → 3개 통과
- 실제 KIS 가능금액 조회(`MSEX`, NASD, $54.53) → `available_usd_capped=66363.01`

## [2026-07-10] 가상 매수 한도 스킵 로그 보강

### 배경
- 해외 모의투자 정규장 외에는 실제 주문 대신 가상 매수로 전략을 평가한다.
- 가능금액 캡을 보수화하면 기존 가상 포지션 노출이 한도를 초과해 신규 가상 매수가
  스킵될 수 있는데, 기존에는 반환값만 있고 `trade_skip` 로그가 남지 않아
  다음 분석 시 "왜 매수가 안 됐는지" 추적이 어려웠다.

### 수정 사항
- `liquidity_lab.py`
  - `_record_trade_skip()`에 선택적 `extra_detail` 인자 추가
  - `_record_virtual_overseas_buy()`에서 `virtual_exposure_limit`,
    `slot_budget_insufficient` 발생 시 `cycle_log`와 `event_log`에 스킵 기록
  - 스킵 detail에 `available_usd`, `virtual_notional_usd`,
    `remaining_virtual_budget` 저장

### 검증
- `python3 -m pytest tests/test_liquidity_lab.py::test_virtual_overseas_buy_respects_total_virtual_exposure_limit -q` → 1개 통과

## [2026-07-10] 청산 정책 컬럼 누락 보강

### 배경
- 최근 7일 `cycle_log` 성과 분석에서 `strategy_flag`는 채워졌지만
  `exit_by`가 대부분 `-`로 남아 있었다.
- 실제 청산은 VWAP/VOL/RSI 전략 매니저의 SELL 신호보다
  `stop_loss`, `atr_hard_stop`, `trend_filter_lost`, `time_exit_profit`
  같은 정책 청산에서 주로 발생한다.
- `exit_by`가 비면 전략별/청산정책별 성과 분석이 어려워진다.

### 수정 사항
- `liquidity_lab.py`
  - 국내/해외 매도 주문에서 전략 매니저의 `exit_by`가 비어 있으면
    저장용으로 `exit_reason`을 fallback 사용
  - 알림 라벨에서는 `exit_by == exit_reason`이면 중복 표기하지 않도록 처리
- `tests/test_liquidity_lab.py`
  - 국내/해외 `SELL_REAL` cycle_log에 청산 정책이 `exit_by`로 저장되는지 검증

### 검증
- `python3 -m pytest tests/test_liquidity_lab.py::test_place_overseas_sell_order_saves_realized_pnl_cycle_log tests/test_liquidity_lab.py::test_place_domestic_sell_order_saves_realized_pnl_cycle_log tests/test_liquidity_lab.py::test_place_overseas_sell_order_sends_telegram_on_success tests/test_liquidity_lab.py::test_place_domestic_sell_order_sends_telegram_on_success -q` → 4개 통과

## [2026-07-10] 해외 실매수 슬롯 산정 기준가 보정

### 발견
- 국내 매수는 `best_ask` 기준으로 슬롯 수량을 계산하지만,
  해외 실매수는 `last_price`로 가능금액/슬롯 수량을 계산한 뒤
  실제 주문은 `ask` 가격으로 제출하고 있었다.
- 스프레드가 작아도 `$100` 예산에서 `last=$25.00`, `ask=$25.01`이면
  기존 로직은 4주(`$100.04`)를 주문할 수 있어 슬롯 예산을 소폭 초과한다.

### 수정 사항
- `liquidity_lab.py`
  - 해외 실제 매수 `_place_overseas_test_order()`에서 주문 예정가(`ask` 우선)를
    먼저 계산
  - KIS 주문가능 조회와 슬롯 수량 계산 모두 실제 주문 예정가 기준으로 수행
- `tests/test_liquidity_lab.py`
  - `$1000 x 10%` 예산, `ask=$25.01`이면 3주로 산정되는지 검증
  - KIS 가능금액 조회 가격이 `25.0100`으로 전달되는지 검증

### 검증
- `python3 -m pytest tests/test_liquidity_lab.py::test_overseas_buy_uses_slot_sizing_when_balance_is_available tests/test_liquidity_lab.py::test_overseas_buy_saves_buy_real_cycle_log tests/test_liquidity_lab.py::test_virtual_overseas_buy_uses_slot_sizing_when_balance_is_available -q` → 3개 통과

## [2026-07-10] `/lab_status` live 미체결 요약 추가

### 배경
- 과거 `BBIO`, `ALNY`, `PCAP`, `073240`처럼 기존 미체결 주문 때문에
  신규 주문/청산 주문이 막힌 사례가 반복됐다.
- `/lab_orders`에서는 live 미체결을 볼 수 있지만, 사용자가 가장 자주 확인하는
  `/lab_status`에는 미체결 요약이 없어 문제를 늦게 발견할 수 있었다.

### 수정 사항
- `telegram_control.py`
  - `/lab_status` 처리 경로를 `_send_status_message()`로 분리
  - 상태 조회 시 live 국내/해외 미체결 주문 개수를 최대 8초 안에 조회
  - 조회 성공 시 `미체결=국내 N / 해외 M` 표시
  - 미체결이 있으면 `/lab_orders`, `/lab_cancel_stale_domestic`,
    `/lab_cancel_stale_overseas` 안내를 함께 표시
  - 조회 실패 시에도 기존 상태 메시지는 보내고 `미체결=조회실패`만 표시
- `tests/test_telegram_control.py`
  - 상태 메시지에 live 미체결 개수와 후속 명령이 표시되는지 검증
  - `_send_status_message()`가 live 조회 결과를 포함해 전송하는지 검증

### 검증
- `python3 -m pytest tests/test_telegram_control.py::test_build_status_message_shows_stopped_loop_notice tests/test_telegram_control.py::test_build_status_message_shows_live_open_order_counts tests/test_telegram_control.py::test_send_status_message_includes_live_open_order_counts -q` → 3개 통과

## [2026-07-10] 모의투자 미국 확장세션 상태 문구 명확화

### 배경
- 모의투자(`vps`)는 미국 프리마켓/애프터마켓에서 KIS 주문이 불가하지만,
  `/lab_status`는 `US premarket (감시중)`처럼 표시해 사용자가 주문 가능 상태로
  오해할 수 있었다.

### 수정 사항
- `telegram_control.py`
  - 실전(`prod`)이 아닌 환경에서 미국 프리마켓/애프터마켓이면
    `US premarket (모의 주문불가·감시만)`처럼 표시
- `tests/test_telegram_control.py`
  - 모의투자 프리마켓 상태 문구 회귀 테스트 추가

### 검증
- `python3 -m pytest tests/test_telegram_control.py::test_build_status_message_marks_mock_us_extended_session_not_orderable tests/test_telegram_control.py::test_build_status_message_shows_stopped_loop_notice -q` → 2개 통과

## [2026-07-10] 계정 만료 전 전방위 점검 — status 위험 가시성/서비스 안정화

### 점검 결과
- 현 서비스는 `kinvest-telegram-control.service`가 `active`이고 거래 루프는
  `stopped` 상태다. 새 매매를 재개하려면 `/lab_start`가 필요하다.
- DB 기준 가상 포지션 15종목, 해외 가상 노출 약 `$393,294.92`가 남아 있었다.
  최근 주문가능 USD 기반 가상 한도(`max_virtual_exposure_pct=0.5`) 대비 초과 상태다.
- 최근 이벤트 로그에서 `MSEX`의 `no_orderable_qty`가 반복되어,
  매도 가능 수량/미체결/정산 지연 문제를 status에서 바로 볼 필요가 있었다.
- systemd 재시작 시 정상 종료인데도 과거 `SystemExit(0)` 스택트레이스가 journal에
  남을 수 있었고, 서비스 재시작 때 `TELEGRAM_CONTROL_START` 알림도 반복될 수 있었다.

### 수정 사항
- `telegram_control.py`
  - `/lab_status`에 `가상노출=... 상태=초과 감시=중지 확인=/lab_portfolio` 한 줄 추가
  - `/lab_status`에 최근 12시간 반복 매도장애 요약 추가
    (`매도가능0`, `주문거부`, 확인 명령 `/lab_orders`)
  - `SIGTERM` 핸들러가 `SystemExit`를 던지지 않고 stop event로 정상 종료되도록 변경
  - `TELEGRAM_CONTROL_START` 시작 알림은 10분 내 재시작 시 생략하도록 throttle 추가
  - 시작 알림 마지막 전송 시각을 `state/runtime_state.json`에 저장/복구
- `liquidity_lab.py`
  - KIS 모의 미국 확장세션 차단 에러 문구는 현재 시각 판정과 무관하게
    `session_not_orderable_in_profile`로 분류해 `order_rejected` 오분류를 방지
- `README.md`
  - `/lab_status`가 가상 노출과 반복 매도장애를 함께 보여준다는 설명 추가

### 검증
- `/lab_status` 실제 DB 렌더링:
  - `가상노출=해외 $393,294.92 15종목 상태=초과 감시=중지 확인=/lab_portfolio`
  - `매도장애(12h)=해외 MSEX 매도가능0 48회 ... 확인=/lab_orders`
- `python3 -m pytest tests -q` → 392개 통과
- 연속 `systemctl --user restart kinvest-telegram-control.service` 후 서비스 `active`
- 재시작 journal에서 신규 `SystemExit` 스택트레이스 미발생 확인
- 10분 내 재시작 시 `telegram_control_start_notified_at`이 갱신되지 않아
  시작 알림 throttle 경로 동작 확인

### 커밋
- `18c64eb` Show virtual exposure in status
- `27c123b` Handle telegram service shutdown gracefully
- `0162fd8` Surface recent sell blocks in status
- `3d07407` Document status risk summaries
- `049c68f` Throttle telegram startup notifications

## [2026-07-10] `/lab_status` 신호 캐시 요약 추가

### 배경
- 거래 루프가 `stopped`인 상태에서 마지막 감시 데이터는 286분 이상 지연되어 있었고,
  마지막 watch target 16개 모두 `stale_signal_cache` 기반이었다.
- 기존 `/lab_status`는 감시데이터 지연 시간은 표시했지만,
  개별 감시 종목 신호가 캐시 기반인지 여부는 `/lab_watchlist`를 봐야 알 수 있었다.

### 수정 사항
- `telegram_control.py`
  - `/lab_status`에 `신호캐시=16/16 전체 캐시 확인=/lab_watchlist` 형태의 요약 추가
  - 일부 종목만 캐시 기반이면 `일부 캐시`로 표시
- `tests/test_telegram_control.py`
  - stale signal cache watch target이 status에 요약되는지 검증

### 검증
- 실제 runtime 렌더링:
  - `감시데이터=288분 전 (루프 stopped)`
  - `신호캐시=16/16 전체 캐시 확인=/lab_watchlist`
- `python3 -m pytest tests -q` → 393개 통과

### 커밋
- `a1057f5` Show stale signal cache count in status

## [2026-07-10] 보유 잔상 표시 정비 및 MSEX 잔고 검증 정정

### 배경
- 초기 DB-only 점검에서 `virtual_positions`와 `virtual_sell_pending`에 없는
  `MSEX`를 닫힌 보유 잔상으로 오판했다.
- 이후 KIS 해외 잔고 API(`overseas-balance-check`)로 검증한 결과,
  `MSEX`는 실제 모의계좌에 522주 존재했고 주문가능수량도 522주였다.
- 교훈: 실보유 여부는 `lab_symbol_state`/virtual 테이블이 아니라
  KIS 잔고 API가 최종 기준이어야 한다.

### 수정 사항
- `liquidity_lab.py`
  - orphan `virtual_sell_pending` 삭제만으로는 `lab_symbol_state` 보유 플래그를
    자동으로 닫지 않도록 보수화
- `telegram_control.py`
  - 마지막 리포트의 watch target이 보유 수량을 갖고 있더라도,
    최신 `lab_symbol_state.has_position=0`이면 `/lab_watchlist`에서 숨김
  - 숨겨진 항목은 `숨김=정리된 보유잔상 N개`로만 간단히 표시
  - 최신 `lab_symbol_state`가 실제 보유 수량/가격/손익을 갖고 있으면
    오래된 마지막 리포트의 watchlist 표시값보다 우선
  - `/lab_portfolio`의 실보유/가상보유 표시도 같은 최신 가격 lookup을 사용
  - `/lab_status`의 `감시수`와 `신호캐시` 요약도 같은 숨김 기준을 적용해
    닫힌 잔상 수를 별도 표기
- 운영 DB
  - 오판 전 백업: `data/trading_backup_20260710_135858_pre_msex_lab_state_cleanup.db`
  - 복구 전 백업: `data/trading_backup_20260710_141256_pre_msex_live_balance_restore.db`
  - `MSEX` lab 상태를 KIS 잔고 기준으로 복구
    (`수량=522`, `주문가능=522`, `평균=$54.1040`, `현재=$54.8800`)

### 검증
- `python3 main.py --settings config/fixed_config.json overseas-balance-check` →
  `MSEX` 522주, `ord_psbl_qty=522` 확인
- 운영 DB의 `lab_symbol_state`에서 `MSEX has_position=1`, `holding_qty=522`로 복구됨
- 운영 `/lab_watchlist` 렌더링에서 `MSEX` 가격 `$54.8800`, 손익 `+1.43%`로 표시됨
- 운영 `/lab_portfolio` 렌더링에서 `MSEX` 실보유/가상보유 현재가와 손익도
  `$54.8800`, `+1.43%`로 표시됨
- `python3 -m pytest tests/test_unified_position_tracker.py::test_reconcile_clears_orphan_virtual_sell_pending tests/test_telegram_control.py::test_build_watchlist_message_hides_closed_stale_position_state tests/test_telegram_control.py::test_build_watchlist_message_uses_balance_cache_for_held_pnl -q` → 3개 통과
- `python3 -m pytest tests -q` → 399개 통과

## [2026-07-10] stale signal cache 신규 매수 차단

### 배경
- 거래 루프가 장시간 `stopped`였고, 마지막 watch target 대부분이
  `stale_signal_cache` 기반이었다.
- 보유 종목은 stale cache라도 청산 리스크 감시에 참고할 수 있지만,
  미보유 종목의 신규 매수까지 오래된 캐시로 허용하면 재개 직후 잘못된 진입이
  발생할 수 있다.

### 수정 사항
- `liquidity_lab.py`
  - 실시간 신호 로딩 실패로 persisted snapshot을 사용하는 fallback 경로에서,
    미보유 종목의 BUY 신호는 `WAIT`로 낮춤
  - 표시 사유는 `[전략] stale_signal_cache_buy_blocked`로 남겨 원인 추적 가능
  - 보유 종목의 stale cache 기반 SELL/HOLD 판단은 유지

### 검증
- `python3 -m pytest tests/test_liquidity_lab.py::test_build_watch_target_status_blocks_cached_buy_for_flat_symbol tests/test_liquidity_lab.py::test_build_watch_target_status_blocks_cached_overseas_standalone_vwap tests/test_liquidity_lab.py::test_build_watch_target_status_allows_overseas_vwap_combo -q` → 3개 통과

## [2026-07-10] 지시문 #65 — 전략 효과 검증 + 매매 빈도 정상화

### 목적
- Codex 70+ 커밋 이후 보수화된 전략의 실거래 효과를 기준일 전후로 검증할 수 있게 함
- RSI 30.0 / VWAP 단독 차단 / min_hold=12가 매매 빈도를 과도하게 낮추는지 감시
- 07/10 기준 국내 전략 성과가 상대적으로 양호해 국내 동시 주문 슬롯 확대

### 수정
- `scripts/analyze_trades.py`
  - `--compare-date YYYY-MM-DD` 옵션 추가
  - 기준일 전후 `SELL_REAL` 전략 성과 비교 출력
- `telegram_control.py`
  - `/lab_report compare 2026-07-10` 명령 추가
  - 텔레그램에서 전략 전후 비교를 즉시 확인 가능
- `liquidity_lab.py`
  - 50사이클마다 유효 매매 빈도 모니터링
  - 사이클당 매매율 1% 미만이면 `low_trade_frequency` 이벤트 저장
  - 해외 RSI 감시 신호가 임계값 초과로 차단되는 횟수 누적 로그
  - 200사이클마다 최근 24시간 `trend_filter_lost` 청산 비율 점검
- `config/fixed_config.json`
  - `max_concurrent_domestic_orders`: 5 → 8
  - `_strategy_changes` 메타데이터 추가

### 평가 기준 (1주 후 재평가)
- 해외 Net EV > -0.1%, 국내 Net EV > +0.1%
- `trend_filter_lost` 비율 < 50%
- 하루 CB 발동 ≤ 2회
- 사이클당 매매율 2~5%

## [2026-07-10] 해외 단독 전략 성과 기반 진입 가드

### 배경
- 최근 2일 `SELL_REAL` 기준 국내는 +106,966원, 해외는 -6,178,536원으로 차이가 컸다.
- 해외 손실은 단독 전략의 `trend_filter_lost` 청산에 집중됐다.
  - 해외 `VWAP` + `trend_filter_lost`: 15건, 승률 0%, 누적 약 -356만원
  - 해외 `RSI` + `stop_loss/trend_filter_lost`: 최근 손실 확대
- 기존 `overseas_block_standalone_vwap`은 고정 차단이지만,
  RSI/VOL 등 다른 단독 전략 부진을 데이터 기반으로 자동 제어하는 장치가 없었다.

### 수정
- `repository.py`
  - `get_recent_strategy_guard_performance()` 추가
  - 최근 `SELL_REAL`을 시장/전략별로 집계하고 평균 순손익률을 계산
- `liquidity_lab.py`
  - `strategy_guard_enabled` 설정 기반 진입 가드 추가
  - 최근 48시간, 최소 3건, 평균 순손익률 -0.3% 이하인 해외 단독 전략은 신규 BUY 차단
  - 차단 발생 시 `strategy_guard_active` 이벤트 기록
  - 후보 선정 단계와 주문 직전 단계 모두에서 같은 가드 적용
- `config/fixed_config.json`
  - 기본 감시 대상: 해외 `VWAP`, `RSI`
  - 조합 전략(`VWAP+RSI`)은 별도 전략으로 취급하여 자동 차단 대상에서 제외
- 2026-07-10 추가 보강
  - 해외 `VOL`도 성과 기반 자동 차단 감시 대상에 포함
  - 현재 VOL은 1건뿐이라 즉시 차단되지 않지만, 3건 이상 누적 후 평균 순손익률이
    기준 이하로 내려가면 신규 진입을 자동 차단

### 의도
- 해외 단독 전략이 다시 회복되기 전까지 신규 진입을 줄여 손실 반복을 방지
- 국내/조합 전략처럼 상대적으로 성과가 나은 경로는 열어두어 매매 빈도 전체가 완전히 죽지 않게 함

## [2026-07-10] 주문 접수/체결확정 구분 강화

### 배경
- 운영 DB의 `broker_order_events`는 KIS 주문 접수/취소/거부 이벤트를 기록하지만,
  실제 체결 확정 이벤트는 별도로 수집하지 않는다.
- 이 때문에 `/lab_orders`와 성과 분석에서 `SUBMITTED` 주문이 실제 체결처럼
  오해될 수 있고, MTS의 체결가 0.000 표시/미체결 주문 상황을 빠르게 판별하기 어렵다.

### 수정
- `repository.py`
  - `list_submitted_order_audit_rows()` 추가
  - 실주문 `SUBMITTED` 중 내부 DB만으로 체결 확정이 불가능한 주문을 감사 대상으로 조회
  - 취소 완료 주문은 제외하고, 취소 거부 주문은 후속 상태와 함께 유지
- `telegram_control.py`
  - `/lab_orders`에 `접수 후 체결확정 추적 필요` 섹션 추가
  - 각 주문에 `확인필요=MTS/잔고`를 표시하여 접수 기록과 체결 확정을 분리
  - KIS live 미체결 조회가 성공한 경우 주문번호를 대조해
    `브로커상태=미체결` 또는 `브로커상태=미체결목록없음`을 함께 표시

### 검증
- 실제 운영 DB 기준 최근 `SUBMITTED` 주문 감사 대상이 조회됨
- `python3 -m pytest tests -q` → 410개 통과

## [2026-07-11] `/lab_guard` 전략 가드 상태 명령 추가

### 배경
- 성과 기반 전략 guard는 내부적으로 작동하지만, 텔레그램에서 현재 어떤 전략이
  차단/감시/참고 상태인지 즉시 확인할 명령이 없었다.
- 해외 단독 전략 손실이 반복되는 상황에서는 차단 기준과 현재 집계 상태를 운영자가
  빠르게 확인할 수 있어야 한다.

### 수정
- `telegram_control.py`
  - `/lab_guard` 명령 추가
  - 최근 `strategy_guard_lookback_hours` 동안의 `SELL_REAL` 성과를
    시장/전략별로 표시
  - `strategy_guard_min_trades`, `strategy_guard_max_avg_net_pnl_pct`,
    감시 시장/전략 목록을 함께 표시
  - 각 전략을 `차단`, `감시`, `참고` 상태로 구분

### 검증
- `/lab_guard` 테스트에서 해외 VWAP 3건 손실 → `상태=차단` 표시 확인

## [2026-07-11] stopped 상태 watchlist 저장값 표시 강화

### 배경
- 텔레그램 컨트롤러 서비스는 실행 중이어도 거래 루프가 `stopped`이면 새 감시
  사이클이 돌지 않는다.
- 이 상태에서 `/lab_watchlist`는 마지막 저장 감시 데이터를 보여주는데, 사용자가
  현재 실시간 감시 목록으로 오해할 수 있었다.

### 수정
- `telegram_control.py`
  - 루프가 running이 아닐 때 감시 데이터 freshness를
    `저장값·루프 중지/일시정지`로 표시
  - `/lab_watchlist` 상단에
    `주의=루프가 실행 중이 아니므로 아래 목록은 마지막 저장 감시데이터` 문구 추가

### 검증
- 현재 운영 runtime 기준 `/lab_watchlist` 샘플에서
  `감시데이터=... (저장값·루프 중지)`와 주의 문구 표시 확인

## [2026-07-11] 해외 청산 가격 쇼크 기준값 보강

### 배경
- PLBL 가상 청산 사례처럼 감시 상태 저장이 먼저 실행되면
  `lab_symbol_state.last_price`가 같은 사이클의 급락/오염 가격으로 덮일 수 있다.
- 이 경우 기존 가격 쇼크 가드가 이전 가격이 아니라 방금 저장된 가격과 비교해
  비정상 가격을 첫 사이클에 통과시킬 위험이 있었다.

### 수정
- `liquidity_lab.py`
  - 해외 보유/가상보유 포지션의 직전 저장 가격을 사이클 초입에
    `_cycle_exit_reference_prices`로 스냅샷
  - `_overseas_exit_price_guard_reason()`이 이 스냅샷을 최우선 기준가격으로 사용
  - DB 값이 이미 현재 가격으로 덮인 경우에도 평균단가 기준 방어가 남도록 조정
- `tests/test_liquidity_lab.py`
  - DB의 현재 가격이 이미 급락 가격으로 덮인 상황에서도, 사이클 기준가격으로
    첫 청산을 보류하고 2회 확인 후 통과하는 회귀 테스트 보강

### 검증
- `python3 -m pytest tests/test_liquidity_lab.py::test_overseas_exit_price_shock_requires_confirmation -q`
  → 통과
- `python3 -m pytest tests -q` → 431개 통과

## [2026-07-11] `/lab_trim_virtual` 가상보유 초과분 정리 명령 추가

### 배경
- 가상보유 종목 수가 `max_concurrent_overseas_orders`를 초과하면 신규 해외 진입이
  막히지만, 기존 `/lab_reset`은 전체 가상거래 이력을 초기화하는 거친 선택지였다.
- 운영 중에는 초과분만 정리해 포지션 한도를 회복하고, 전략 성과 통계는 오염시키지
  않는 중간 명령이 필요했다.

### 수정
- `telegram_control.py`
  - `/lab_trim_virtual` 추가: 초과분 정리 후보와 확인 명령 표시
  - `/lab_trim_virtual_confirm` 추가: 손익률이 낮고 오래된 해외 가상보유 초과분만
    전량 가상매도로 기록 후 `virtual_positions`에서 삭제
  - 정리 전 DB 백업 생성
  - 정리 매도는 `reason=manual_virtual_trim`,
    `excluded_from_performance=1`로 저장해 전략 성과에서 제외
- `tests/test_telegram_control.py`
  - 파서 테스트 및 초과분만 정리되는 동작 테스트 추가

### 검증
- `python3 -m pytest tests/test_telegram_control.py::test_parse_command tests/test_telegram_control.py::test_trim_virtual_prompt_and_confirm_closes_excess_positions -q`
  → 통과
- `python3 -m pytest tests -q` → 432개 통과

## [2026-07-11] 해외 VOL 단독 진입 고정 차단

### 배경
- 최근 48시간 성과 기준 해외 단독 전략은 모두 부진했다.
  - 해외 VWAP: 14건, 평균 순손익률 -0.63%
  - 해외 RSI: 4건, 평균 순손익률 -2.02%
  - 해외 VOL: 1건, 평균 순손익률 -0.87%
- VWAP/RSI 단독은 이미 고정 차단 중이었지만, VOL 단독은 3건 누적 전이라
  성과 기반 guard가 아직 차단하지 못했다.
- 국내 전략은 같은 기간 VWAP/VOL/VWAP+RSI가 양호하므로 국내 정책은 유지한다.

### 수정
- `config.py`, `config/fixed_config.json`
  - `overseas_block_standalone_vol=true` 추가
- `liquidity_lab.py`
  - 해외 단독 `VOL` BUY 신호를 `standalone_vol_blocked`로 WAIT 처리
- `telegram_control.py`
  - `/lab_guard` 고정차단 표시 대상에 `해외 VOL단독` 추가
- `tests`
  - 설정 로드 및 해외 VOL 단독 차단 테스트 추가

### 검증
- `python3 -m pytest tests/test_config.py::test_load_app_config_uses_paper_profile_variables tests/test_liquidity_lab.py::test_build_watch_target_status_blocks_overseas_standalone_vol -q`
  → 통과
- `python3 -m pytest tests -q` → 433개 통과

## [2026-07-11] 가상보유 초과 안내 문구 개선

### 수정
- `/lab_status`, `/lab_portfolio`에서 가상 포지션 한도 초과 시
  전체 초기화(`/lab_reset`)보다 초과분 정리(`/lab_trim_virtual`)를 우선 안내하도록 변경

### 검증
- `python3 -m pytest tests/test_telegram_control.py::test_build_status_message_shows_virtual_position_cap tests/test_telegram_control.py::test_build_portfolio_message_shows_virtual_position_cap -q`
  → 통과
- `python3 -m pytest tests -q` → 433개 통과

## [2026-07-11] `/lab_status` 매도장애 최근 발생 시각 표시

### 배경
- `매도장애(12h)`는 최근 12시간 `event_log` 집계라서, 이미 9시간 지난
  과거 장애도 현재 진행 중인 장애처럼 보일 수 있었다.
- MSEX `매도가능0` 사례도 마지막 이벤트가 약 9시간 전이었으나 상태 메시지에는
  경과 시간이 표시되지 않았다.

### 수정
- `telegram_control.py`
  - 매도장애 항목에 `최근=방금/17분전/9시간전/1일전` 형식의 경과 표시 추가
- `tests/test_telegram_control.py`
  - 매도장애 상태 메시지와 경과 포맷 테스트 추가

### 검증
- 운영 DB 기준 상태 메시지:
  `매도장애(12h)=해외 MSEX 매도가능0 20회 최근=9시간전 ...`
- `python3 -m pytest tests/test_telegram_control.py::test_build_status_message_shows_recent_sell_block_events tests/test_telegram_control.py::test_format_recent_age_text -q`
  → 통과
- `python3 -m pytest tests -q` → 434개 통과

## [2026-07-11] `/lab_trim_virtual` stale 가격 정리 방지

### 배경
- 현재 거래 루프가 `stopped`라 가상보유 정리 후보의 `lab_symbol_state.last_price`가
  약 8시간 전 저장값이었다.
- 기존 `/lab_trim_virtual_confirm`는 이 저장가만으로도 가상 포지션을 삭제할 수 있어,
  실제 현재가와 다른 가격으로 수동 정리될 위험이 있었다.

### 수정
- `telegram_control.py`
  - `/lab_trim_virtual` 실행 시 live quote lookup을 먼저 수행
  - 프롬프트에 `가격소스=live N건`과 저장가 나이를 표시
  - `/lab_trim_virtual_confirm`는 live 현재가가 확보된 후보만 정리
  - live 현재가가 없으면 `정리보류` 메시지를 보내고 가상 포지션을 보존
- `tests/test_telegram_control.py`
  - live 가격 기반 정리 성공 테스트 갱신
  - live 가격 미확보 시 prompt/confirm 보류 테스트 추가

### 검증
- 운영 DB 기준 live 미확보 시:
  `가상보유 초과분 정리 보류`, `저장가=8시간전`, `조치=/lab_start 후 재조회`
- `python3 -m pytest tests/test_telegram_control.py::test_trim_virtual_prompt_and_confirm_closes_excess_positions tests/test_telegram_control.py::test_execute_trim_virtual_defers_when_live_prices_missing -q`
  → 통과
- `python3 -m pytest tests -q` → 435개 통과

## [2026-07-11] 장열림·보유중 자동감시 중지 경고 추가

### 배경
- 현재 서비스 프로세스는 active지만 거래 루프는 `stopped` 상태이고, 미국 정규장이
  열려 있으며 실보유/가상보유가 남아 있다.
- 기존 `/lab_status`는 `거래루프=중지됨`과 가상노출을 별도 줄로 보여줬지만,
  장이 열린 상태에서 자동 청산 감시가 꺼져 있다는 위험을 한눈에 보기 어려웠다.

### 수정
- `telegram_control.py`
  - KRX/US 세션이 열려 있고 보유 또는 가상보유 종목이 있는데 루프가 running이
    아니면 `주의=US 장열림·보유 N종목, 자동감시 중지 조치=/lab_start` 표시
  - 실보유와 가상보유를 고유 종목 기준으로 합산
- `tests/test_telegram_control.py`
  - stopped 상태 경고 표시 및 running 상태에서는 숨김 테스트 추가

### 검증
- 운영 DB 기준 상태 메시지:
  `주의=US 장열림·보유 16종목, 자동감시 중지 조치=/lab_start`
- `python3 -m pytest tests/test_telegram_control.py::test_build_stopped_open_market_warning_counts_real_and_virtual_positions tests/test_telegram_control.py::test_build_stopped_open_market_warning_hidden_while_running -q`
  → 통과
- `python3 -m pytest tests -q` → 437개 통과

## [2026-07-11] `/lab_start` 가상 포지션 한도 초과 안내

### 배경
- 현재 운영 DB 기준 해외 가상보유가 15종목이고 `max_concurrent_overseas_orders=8`이라
  `/lab_start` 후에도 신규 해외 매수는 한도 해소 전까지 제한된다.
- 기존 시작/재개 응답은 미체결 주문만 경고해, 사용자가 시작 후 해외 매수가 안 되는
  이유를 즉시 알기 어려웠다.

### 수정
- `telegram_control.py`
  - `/lab_start`, `/lab_resume` 응답에 가상 포지션 한도 초과 시
    `가상포지션=해외 N/M 초과`, `신규해외매수=한도 해소 전 제한`,
    `정리=/lab_trim_virtual` 안내 추가
- `tests/test_telegram_control.py`
  - 시작/재개 명령에서 가상 포지션 한도 초과 안내가 표시되는 테스트 추가

### 검증
- 운영 DB 기준 시작 안내 시뮬레이션:
  `가상포지션=해외 15/8 초과`, `신규해외매수=한도 해소 전 제한`
- `python3 -m pytest tests/test_telegram_control.py::test_handle_start_like_command_warns_about_virtual_position_cap tests/test_telegram_control.py::test_handle_start_like_command_warns_about_live_open_orders -q`
  → 통과
- `python3 -m pytest tests -q` → 438개 통과

## [2026-07-11] `/lab_orders` 체결확정 감사 경과 표시

### 배경
- 운영 DB에 국내 `SUBMITTED` 주문 접수 기록이 다수 남아 있고, DB만으로는
  실제 체결 확정 여부를 알 수 없다.
- 기존 `/lab_orders`의 `접수 후 체결확정 추적 필요` 섹션은 주문 시각은 보여줬지만,
  경과 시간이 직접 표시되지 않아 오래된 접수 기록인지 즉시 판단하기 어려웠다.

### 수정
- `telegram_control.py`
  - 체결확정 감사 라인에 `경과=11시간26분`, `주의=장기미체결` 형식 표시 추가
- `tests/test_telegram_control.py`
  - 주문 감사 메시지 테스트를 새 경과 표시 포맷에 맞게 갱신

### 검증
- 운영 DB 기준 `/lab_orders` 샘플:
  `국내 360750 매도접수 ... 확인필요=MTS/잔고 경과=11시간26분 주의=장기미체결`
- `python3 -m pytest tests/test_telegram_control.py::test_build_recent_order_events_message_marks_audit_order_live_open_status tests/test_telegram_control.py::test_build_recent_order_events_message_formats_submission_cancel_and_virtual -q`
  → 통과
- `python3 -m pytest tests -q` → 438개 통과
