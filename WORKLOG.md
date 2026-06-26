# WORKLOG

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
