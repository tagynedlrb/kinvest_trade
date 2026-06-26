# KIS Trade Scaffold

`kiwoom_trade`의 운영 감각을 유지하면서 브로커 연동만 한국투자증권 Open API로 바꾼 단기투자 프로젝트다.  
현재 구조는 `실시간 시세 확인`, `지표 계산`, `paper trading`, `텔레그램 알림`, `모의/실전 계정 분리`, `주문 테스트 CLI`까지 포함한다.
또한 프로세스 간 `KIS 접근토큰 캐시`를 사용해 `1분당 1회` 토큰 발급 제한에 덜 걸리도록 정리했다.
현재 기본 진입점 `python3 main.py` 는 해외주식 `거래량 폭발 + 가격 모멘텀` 자동매매 루프를 바로 실행하도록 연결되어 있다.

## 현재 구조
- `config/fixed_config.json`: 고정 설정
- `state/runtime_state.json`: 최신 실행 상태
- `src/kinvest_trade/client.py`: KIS OAuth, 시세, 잔고, 매수가능조회, 주문
- `src/kinvest_trade/cli.py`: `doctor`, `auth-check`, `balance-check`, `overseas-price-check`, `overseas-balance-check`, `overseas-orderable-check`, `overseas-order-test`, `indicator-check`, `orderable-check`, `order-test`, `paper-run`, `paper-report`, `telegram-test`
- `src/kinvest_trade/cli.py`: `liquidity-lab` 명령으로 국내/해외 고유동성 후보를 스캔하고 국내 paper test + 모의주문 테스트까지 한 번에 수행할 수 있다.
- `src/kinvest_trade/cli.py`: `telegram-control` 명령으로 텔레그램 봇 명령을 받아 `liquidity-lab` 루프를 시작/중지/재개/종료할 수 있다.
- `liquidity-lab`는 현재 열린 시장의 상위 후보 여러 개를 동시에 감시하고, 그중 `볼륨 스파이크 + 돌파` 신호가 뜬 종목만 주문 대상으로 승격한다.
- `run_watch.py`: 옵션 없이 콘솔 감시 실행
- `WORKLOG.md`: 작업 기록

## KIS 기준으로 분리해 둔 값
공식 KIS 샘플도 `실전 앱키/시크릿`과 `모의투자 앱키/시크릿`, `실전 계좌`와 `모의투자 계좌`를 분리해서 관리한다.  
이 프로젝트도 같은 방식으로 맞춰두었다.

### 1. 키 파일 위치
키는 아래 파일 중 활성 프로필에 맞는 파일을 읽는다.

실계좌용:
```text
keys/prod_appkey.txt
keys/prod_appsecret.txt
```

모의투자용:
```text
keys/vps_appkey.txt
keys/vps_appsecret.txt
```

예전 단일 파일 방식도 하위호환으로 남겨두었지만, 이제는 위 파일명을 권장한다.

### 2. `.env`에 넣는 계좌 정보
```bash
cp .env.example .env
```

`.env` 예시:
```text
KIS_ENV=vps

KIS_PROD_ACCOUNT_NO=실계좌번호앞8자리또는10자리전체
KIS_PROD_ACCOUNT_PRODUCT_CODE=01
KIS_PROD_HTS_ID=실계좌_HTS_ID

KIS_VPS_ACCOUNT_NO=모의계좌번호앞8자리또는10자리전체
KIS_VPS_ACCOUNT_PRODUCT_CODE=01
KIS_VPS_HTS_ID=모의계좌_HTS_ID

DRY_RUN=true
LIVE_TRADING_ENABLED=false
```

- `KIS_ENV=prod` 이면 실계좌 세트를 사용한다.
- `KIS_ENV=vps` 이면 모의투자 세트를 사용한다.
- 계좌번호를 10자리 전체로 넣고 상품코드를 비우면 코드가 자동으로 `앞 8자리 + 뒤 2자리`로 분리한다.
- 키를 파일 대신 환경변수로 직접 넣고 싶다면 `KIS_PROD_APPKEY`, `KIS_PROD_APPSECRET`, `KIS_VPS_APPKEY`, `KIS_VPS_APPSECRET`도 지원한다.

## 빠른 시작
1. 의존성 준비
```bash
cd /home/ubuntu/kinvest_trade
sudo apt-get update
sudo apt-get install -y python3-httpx python3-dotenv python3-pytest
```

2. 환경파일 준비
```bash
cp .env.example .env
```

3. 키 입력
```bash
printf '%s\n' '실계좌_appkey' > keys/prod_appkey.txt
printf '%s\n' '실계좌_appsecret' > keys/prod_appsecret.txt
printf '%s\n' '모의_appkey' > keys/vps_appkey.txt
printf '%s\n' '모의_appsecret' > keys/vps_appsecret.txt
chmod 600 keys/prod_appkey.txt keys/prod_appsecret.txt keys/vps_appkey.txt keys/vps_appsecret.txt
```

4. `.env`에 계좌번호 입력 후 설정 확인
```bash
python3 main.py doctor
```

5. 텔레그램 연결 테스트
```bash
python3 run_telegram_test.py
```

## 권장 테스트 순서
처음에는 반드시 `모의투자(vps)`로 진행하는 것을 권장한다.

## SOXL 테스트 메모
- `SOXL`은 해외주식이므로 `indicator-check`, `paper-run`, `run_watch.py` 같은 현재 국내주식 감시 루프와는 별도다.
- `SOXL` 주문용 거래소 코드는 `AMEX`를 사용한다.
- `SOXL` 현재가 조회는 내부적으로 KIS 조회코드 `AMS`로 자동 변환한다.
- KIS `해외주식 상품기본정보(search-info)`는 모의투자에서 지원되지 않아, 모의투자에서는 현재가/잔고/주문가능금액/주문 중심으로 테스트한다.

## 기본 자동 실행
아무 옵션 없이 아래처럼 실행하면 된다.
```bash
python3 main.py
```

이 명령은 `config/fixed_config.json`의 `auto_trade` 정책을 읽어 다음을 수행한다.
- 대상: `SOXL`
- 거래소: `AMEX`
- 계정: 현재 `.env`의 `KIS_ENV`가 가리키는 프로필
- 기본 실행: `중지 전까지` 또는 `장 종료 전까지` 계속
- `max_actions_per_run=0`, `max_decision_cycles_per_run=0`이면 무제한 감시로 해석
- 액션 알림: 체결마다 텔레그램 전송
- 최종 알림: 수동 종료 또는 장 종료 시 손익 요약 텔레그램 전송

현재 기본 정책은 `OVERSEAS_LIQUIDITY_MOMENTUM` 이다.
- 일봉 `20일선/60일선`과 5분봉 `5선/20선`은 `방향 필터`로만 사용한다.
- 실제 진입 트리거는 `분봉 거래량 비율 급증`, `직전 구간 고점 돌파`, `볼린저 상단 돌파`, `짧은 모멘텀 양수` 조합이다.
- 현재 기본값은 `1분봉 + 10초 폴링` 기준이며, 최근 5분 안쪽의 짧은 흐름을 잡아 `수분 내 청산`을 목표로 한다.
- 보유 중에는 `ATR 기반 손절`, `모멘텀 약화`, `볼륨 페이드`, `트레일링 되돌림`, `시간초과`를 나눠서 판단한다.
- `매수 다음이 반드시 매도`가 아니며, 조건이 맞으면 분할매수와 분할매도를 모두 허용한다.
- 기준 턴마다 반드시 거래하지 않고, 조건이 약하면 `skip/pass`로 넘긴다.
- 모의투자에서 매도 잔고 반영이 늦을 경우, 실패 종료하지 않고 다음 사이클에 자동 재시도한다.

### 전략 핵심
- 진입은 `거래량 폭발 + 가격 돌파`가 메인이고, 이평선은 `상승 방향일 때만 롱 허용`하는 필터 역할만 한다.
- 손절은 고정 퍼센트만 쓰지 않고, `ATR * 배수`와 `모멘텀 붕괴`를 함께 본다.
- 익절은 `부분 익절`, `트레일링`, `볼륨 감소`, `과열 RSI`를 함께 써서 한 번에 전량 정리하지 않도록 설계했다.
- `max_position_qty`, `allow_scale_in`, `allow_partial_exit`로 보유 수량을 고정하지 않고 가변적으로 조절한다.
- 기본값은 `slot sizing` 기반이다. `last_available_usd × slot_max_pct`를 슬롯 예산으로 잡고, 진입/추가매수 수량을 달러 금액 기준으로 역산한다.
- `poll_interval_sec=10`으로 시세는 자주 감시하되, 무거운 차트 컨텍스트는 `1분봉 기준`으로 캐시해 호출 수를 억제한다.
- `adaptive_params.py`가 매 사이클마다 ATR, 거래량 강도, 모멘텀을 다시 계산해 `take_profit_pct`, `stop_loss_pct`, `volume_spike_ratio`, `max_hold_cycles`를 동적으로 덮어쓴다.
- 보유 시간이 길어졌을 때는 수익 포지션뿐 아니라 손실 포지션에도 `time_exit_loss`, `time_exit_forced`가 작동해 손실 방치를 줄인다.

### 손익 계산 기준
- 실현 손익은 `gross_pnl_usd`, `net_pnl_usd`, `net_pnl_krw`로 나눠 저장한다.
- `net_pnl_usd`에는 매수/매도 수수료와 미국 매도 `SEC Fee` 추정치를 반영한다.
- `net_pnl_krw`에는 체결 시점 환율 추정치와 환차손익을 반영한다.
- `estimated_tax_krw`는 해외주식 양도소득세를 연간 기본공제 `250만원`과 세율 `22%` 기준으로 현재 런 기준 추정한 값이다.
- `fx_fee_rate`는 사용자 환전 조건에 따라 달라질 수 있어 기본값을 `0.0`으로 두었고, 필요하면 `config/fixed_config.json`에서 직접 조정하면 된다.

## Liquidity Lab
고유동성 후보를 기준으로 국내/해외를 같이 보면서 그날의 테스트 타겟을 자동 선정하려면 아래 명령을 사용한다.

```bash
python3 main.py liquidity-lab
```

이 명령은 다음을 수행한다.
- 국내 후보군과 해외 후보군을 각각 현재 거래대금, 최근 체결량, 스프레드, 단기 체결 모멘텀으로 점수화한다.
- `거래량`은 단독 기준이 아니라, “지금 가장 활발하게 거래되는 종목”을 고르기 위한 보조지표로만 사용한다.
- 흔히 말하는 `개잡주` 성격을 줄이기 위해, 현재 버전은 `저가주 + 얇은 거래대금/거래량 + 넓은 스프레드` 조합의 후보를 자동 제외한다.
- 국내 기본 제외 기준은 `5,000원 미만`, `당일 거래대금 500억 원 미만`, `최근 체결량 합계 3만 미만`, `스프레드 0.3% 초과`다.
- 해외 기본 제외 기준은 `10달러 미만`, `거래량 1만 미만`, `스프레드 0.4% 초과`다.
- 현재 장이 열린 시장에서 `activity_score`가 높은 후보군을 먼저 뽑고, 그 안에서 `signal_score`가 가장 강한 종목을 우선 주문 대상으로 선택한다.
- 해외 후보군은 고정 1종목만 보는 대신, 넓은 벤치 후보군을 두고 `active_pool`만 짧은 주기로 감시한다.
- 기본값은 `해외 후보 20개`, `active_pool 5개`, `4사이클마다 전체 벤치 재스캔`이다.
- 벤치 재스캔 때는 `activity_score` 상위 종목으로 `active_pool`을 교체하고, `POOL_ROTATION` heartbeat를 남긴다.
- 보유 중인 해외 종목은 `active_pool` 밖으로 밀려도 다음 사이클 스캔 대상에 강제로 다시 포함해 청산 신호가 끊기지 않게 한다.
- 실제 해외 주문은 `activity_score`만으로 바로 넣지 않고, 선택된 후보가 `거래량 스파이크 + 돌파 + 추세 필터`를 동시에 만족할 때만 진행한다.
- 다만 해외 mock 포지션이 이미 있고 손절/익절 기준에 먼저 걸린 보유분이 있으면, 신규 매수보다 기존 보유 청산을 우선한다.
- 고정 손절/익절에 먼저 걸리지 않았더라도, 보유 종목이 `ATR 손절`, `모멘텀 약화`, `볼륨 페이드` 신호를 보이면 청산 후보로 올린다.
- 국내 장이 열려 있고 `primary_target`이 국내 종목이면 그 1개 종목만 짧은 `paper-run`과 mock 주문 테스트 대상으로 사용한다.
- 미국장이 열려 있고 `primary_target`이 해외 종목이면 그 1개 종목만 mock 주문 테스트 대상으로 사용한다.

현재 기본 후보군과 개잡주 필터 기준은 `config/fixed_config.json`의 `liquidity_lab` 섹션에서 조정할 수 있다.
- 국내: `005930`, `000660`, `035420`, `419050`, `023410`, `010170`, `034940`
- 해외: `NVDA`, `AAL`, `INTC`, `AMZN`, `MU`, `AMD`, `META`, `TSLA`, `MSFT`, `AAPL`, `PLTR`, `SMCI`, `ARM`, `COIN`, `NFLX`, `F`, `BAC`, `C`, `T`, `XOM`

운영 메모:
- `liquidity-lab` 테스트에서 작은 호가 차익만으로는 국내 mock 왕복 주문 순손익이 음수가 될 수 있었다.
- 따라서 실제 자동전략에는 `예상 엣지 > 왕복 비용` 조건을 반드시 함께 두는 것이 안전하다.

## Telegram Control
텔레그램 봇으로 `liquidity-lab` 테스트 프로그램을 원격 제어하려면 아래 명령으로 컨트롤러를 실행한다.

```bash
python3 main.py telegram-control
```

이 프로세스는 계속 떠 있으면서 텔레그램 봇 명령을 받아 `liquidity-lab`를 반복 실행한다.

백그라운드 서비스로 상주시킬 때는 아래 스크립트를 사용한다.

```bash
bash scripts/install_telegram_control_service.sh
systemctl --user status kinvest-telegram-control.service --no-pager
```

서비스로 올려두면 `liquidity-lab` 테스트가 끝난 뒤에도 컨트롤러는 계속 살아 있고, 텔레그램 명령만 보내면 다시 수행할 수 있다.

지원 명령:
- `/lab_start`: 즉시 루프 시작
- `/lab_pause`: 현재 사이클은 마무리하고 일시정지
- `/lab_resume`: 일시정지 상태에서 재개
- `/lab_stop`: 현재 사이클 취소 요청 후 정지. 그 시점까지의 누적 거래/손익 요약을 텔레그램으로 전송하고 DB에 기록
- `/lab_terminate`: 현재 lab 실행을 강제 종료하고 대기 상태로 복귀. 그 시점까지의 누적 거래/손익 요약을 텔레그램으로 전송하고 DB에 기록
- `/lab_status`: 현재 상태 조회
- `/lab_watchlist`: 현재 감시중인 종목 목록과 `20d/60d`, `5/20` 이평 관계, `vr/mom` 기반 짧은 상태 요약 조회
- `/lab_positions`: 현재 보유 포지션과 미실현 손익 조회
- `/lab_help`: 명령 목록 조회

동작 메모:
- `중지(stop)`는 루프를 멈추지만 컨트롤러 프로세스는 살아 있다.
- `종료(terminate)`도 컨트롤러 서비스는 유지하고, lab 실행만 강제로 끝낸 뒤 명령 대기 상태로 돌아간다.
- `stop`/`terminate` 요약은 `telegram_control_sessions` 테이블에도 저장되어 다음 전략 개선 때 누적 성과를 되짚는 데 사용한다.
- `stop`/`terminate` 요약은 `종목별 buy/sell 횟수`, `domestic paper 실현손익`, `해외 청산 추정손익`까지 함께 묶어 짧게 보여준다.
- 다음 자동 실행 간격은 `config/fixed_config.json`의 `liquidity_lab.loop_interval_sec`으로 조절한다.
- 현재 기본값은 `15초`이며, 다음 실행 시점은 `이전 사이클 종료 후 추가 대기`가 아니라 `이전 사이클 시작 시점 기준`으로 계산해 감시 간격이 불필요하게 늘어지지 않도록 했다.
- 텔레그램 long polling 시간은 `notifications.telegram_command_poll_timeout_sec`으로 조절한다.
- 서비스 로그는 `journalctl --user -u kinvest-telegram-control.service -f`로 확인할 수 있다.
- `WAIT` 상태는 더 이상 텔레그램으로 매 사이클 전송하지 않는다. 텔레그램 알림은 실제 `매수/매도 제출` 또는 `주문 오류` 중심으로만 보낸다.
- 현재 기본 해외 감시는 `overseas_top_n=5`, `active_pool=5`, `bench_scan_every=4` 기준이다. 평소에는 active pool만 짧게 스캔하고, 4사이클마다만 20개 전체를 다시 훑는 구조라 평균 호출량을 낮추면서도 타겟 교체는 유지한다.
- `/lab_watchlist`에서 보유 종목은 `hold=N`과 함께 `pnl=+X.XX%` 형식의 미실현 손익이 함께 표시된다.

### 1. 모의투자 모드로 전환
`.env`에서 아래처럼 둔다.
```text
KIS_ENV=vps
DRY_RUN=true
LIVE_TRADING_ENABLED=false
```

### 2. 인증 확인
```bash
python3 main.py auth-check
```

### 3. 잔고 조회
```bash
python3 main.py balance-check
```

### 4. 시세/지표 조회
```bash
python3 main.py indicator-check 005930 --timeframe minute
python3 main.py indicator-check 005930 --timeframe daily
```

### 5. 매수가능 수량 조회
시장가 기준 예시:
```bash
python3 main.py orderable-check 005930 --price 70000 --order-division 01
```

지정가 기준 예시:
```bash
python3 main.py orderable-check 005930 --price 70000 --order-division 00
```

### 6. 주문 미리보기
아직 제출하지 않고 요청 내용을 확인한다.
```bash
python3 main.py order-test buy 005930 --qty 1 --price 70000 --order-division 00
python3 main.py order-test sell 005930 --qty 1 --price 70000 --order-division 00
```

### 7. 모의투자 실제 주문 테스트
`DRY_RUN=false` 로 바꾼 뒤 실행한다.

모의 매수:
```bash
python3 main.py order-test buy 005930 --qty 1 --price 70000 --order-division 00 --execute
```

모의 매도:
```bash
python3 main.py order-test sell 005930 --qty 1 --price 70000 --order-division 00 --execute
```

시장가 주문 예시:
```bash
python3 main.py order-test buy 005930 --qty 1 --price 0 --order-division 01 --execute
```

### 8. 실데이터 기반 paper trading
이건 실주문이 아니라 `실데이터 + 가상체결`이다.
```bash
python3 main.py paper-run --iterations 20 --interval-sec 15
python3 main.py paper-report
```

## SOXL 기준 해외주식 테스트
모의투자 기준:
```bash
python3 main.py auth-check
python3 main.py overseas-price-check SOXL --exchange AMEX
python3 main.py overseas-balance-check --exchange AMEX --currency USD
python3 main.py overseas-orderable-check SOXL --exchange AMEX --price 220.19
python3 main.py overseas-order-test buy SOXL --exchange AMEX --qty 1 --price 220.19 --order-division 00
DRY_RUN=false python3 main.py overseas-order-test buy SOXL --exchange AMEX --qty 1 --price 220.19 --order-division 00 --execute
DRY_RUN=false python3 main.py overseas-order-test sell SOXL --exchange AMEX --qty 1 --price 220.19 --order-division 00 --execute
```

- 모의투자 미국주식 주문은 `00` 지정가 기준으로 테스트하는 쪽이 안전하다.
- KIS 모의투자는 미국주식 `매도`에서 제약이 있을 수 있어, 실제 응답 메시지를 함께 확인해야 한다.

## 실계좌 전환 방법
실계좌 확인은 마지막 단계에서만 권장한다.

`.env`를 아래처럼 바꾼다.
```text
KIS_ENV=prod
DRY_RUN=true
LIVE_TRADING_ENABLED=false
```

실계좌에서 실제 주문까지 열려면 두 조건이 모두 필요하다.
- `DRY_RUN=false`
- `LIVE_TRADING_ENABLED=true`

그리고 `order-test --execute` 시 추가로 아래 확인문구가 필요하다.
```bash
--confirm-live EXECUTE_LIVE
```

예시:
```bash
python3 main.py order-test buy 005930 --qty 1 --price 70000 --order-division 00 --execute --confirm-live EXECUTE_LIVE
```

## 실시간 감시
```bash
python3 run_watch.py
```

한 번만 시험:
```bash
KIS_WATCH_MAX_CYCLES=1 python3 run_watch.py
```

## 주요 명령
```bash
python3 main.py
python3 main.py auto-run
python3 main.py doctor
python3 main.py auth-check
python3 main.py balance-check
python3 main.py overseas-price-check SOXL --exchange AMEX
python3 main.py overseas-balance-check --exchange AMEX --currency USD
python3 main.py overseas-orderable-check SOXL --exchange AMEX --price 220.19
python3 main.py overseas-order-test buy SOXL --exchange AMEX --qty 1 --price 220.19 --order-division 00
python3 main.py overseas-order-test buy SOXL --exchange AMEX --qty 1 --price 220.19 --order-division 00 --execute
python3 main.py indicator-check 005930 --timeframe minute
python3 main.py indicator-check 005930 --timeframe daily
python3 main.py orderable-check 005930 --price 70000 --order-division 01
python3 main.py order-test buy 005930 --qty 1 --price 70000 --order-division 00
python3 main.py order-test buy 005930 --qty 1 --price 70000 --order-division 00 --execute
python3 run_watch.py
python3 main.py paper-run --iterations 20 --interval-sec 15
python3 main.py paper-report
python3 run_telegram_test.py
```

## 참고한 공식 자료
- KIS API 포털: https://apiportal.koreainvestment.com/
- KIS 공식 샘플 저장소: https://github.com/koreainvestment/open-trading-api
