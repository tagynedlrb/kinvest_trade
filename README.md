# KIS Trade Scaffold

`kiwoom_trade`의 운영 감각을 유지하면서 브로커 연동만 한국투자증권 Open API로 바꾼 단기투자 프로젝트다.  
현재 구조는 `실시간 시세 확인`, `지표 계산`, `paper trading`, `텔레그램 알림`, `모의/실전 계정 분리`, `주문 테스트 CLI`까지 포함한다.
또한 프로세스 간 `KIS 접근토큰 캐시`를 사용해 `1분당 1회` 토큰 발급 제한에 덜 걸리도록 정리했다.
현재 기본 진입점 `python3 main.py` 는 `auto_trade.symbol`에 지정한 해외 종목 1개를 고정 감시하는 `auto-run` 모드로 연결되어 있다.

## 현재 구조
- `config/fixed_config.json`: 고정 설정
- `state/runtime_state.json`: 최신 실행 상태
- `src/kinvest_trade/client.py`: KIS OAuth, 시세, 잔고, 매수가능조회, 주문
- `src/kinvest_trade/auto_trader.py`: `auto_trade.symbol`에 지정한 고정 1종목 자동매매
- `src/kinvest_trade/liquidity_lab.py`: 국내/해외 후보군을 스캔해 가장 활발한 종목을 자동 선정하는 사이클 오케스트레이션 본체
- `src/kinvest_trade/lab_watch.py`: 국내/해외 매수·청산 감시대상 선정, watch target 상태 계산
- `src/kinvest_trade/lab_domestic_orders.py`: 국내 실주문(매수/매도) 제출, 미체결 정정, 체결 로그
- `src/kinvest_trade/lab_overseas_orders.py`: 해외 실주문(매수/매도) 제출, 가상매수/가상매도 fallback, 미체결 정정, 체결 로그
- `src/kinvest_trade/lab_positions.py`: 실보유/가상보유 통합 포지션 트래커, 가상거래 관리자
- `src/kinvest_trade/lab_runtime.py`: 쿨다운/재시도/체결확정 대기 등 사이클 간 런타임 상태
- `src/kinvest_trade/lab_risk.py`: 연속손절·일일손실 서킷브레이커, 주문거부 서킷브레이커 상태 관리
- `src/kinvest_trade/lab_notify.py`: 거래 알림 큐/배치 전송
  - (위 `lab_*.py`는 원래 `liquidity_lab.py` 한 파일이었으나, 8,700줄을 넘기며 유지보수가
    어려워져 성격별로 분리했다. 각 파일은 `LiquidityLabService` 인스턴스를 `service`로 받아
    위임받는 helper 클래스 형태이며, `liquidity_lab.py`에는 얇은 wrapper 메서드만 남아있다.)
- `src/kinvest_trade/cli.py`: `auto-run`, `liquidity-lab`, `telegram-control`, `doctor`, `auth-check`, `balance-check`, `overseas-price-check`, `overseas-balance-check`, `overseas-orderable-check`, `overseas-order-test`, `indicator-check`, `orderable-check`, `order-test`, `paper-run`, `paper-report`, `telegram-test`
- `src/kinvest_trade/telegram_control.py`: 텔레그램 봇 명령으로 `liquidity-lab` 루프를 시작/중지/재개/종료, 상태·리포트 메시지 생성
- `src/kinvest_trade/git_uploader.py`: `/lab_gitlog` 명령으로 당일 거래/이벤트/주문/텔레그램/API 호출 로그를 GitHub에 CSV 업로드
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

키 파일 생성 예시:
```bash
# 실계좌 앱키/시크릿을 파일로 저장 (실전투자용, 신중하게 다룰 것)
printf '%s\n' '실계좌_appkey' > keys/prod_appkey.txt
printf '%s\n' '실계좌_appsecret' > keys/prod_appsecret.txt
# 모의투자 앱키/시크릿을 파일로 저장 (개발/검증용, 먼저 이것으로 테스트)
printf '%s\n' '모의_appkey' > keys/vps_appkey.txt
printf '%s\n' '모의_appsecret' > keys/vps_appsecret.txt
# 키 파일 권한을 본인만 읽기/쓰기 가능하도록 제한 (보안)
chmod 600 keys/prod_appkey.txt keys/prod_appsecret.txt keys/vps_appkey.txt keys/vps_appsecret.txt
```

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

3. 위 `KIS 기준으로 분리해 둔 값` 섹션에 따라 키 파일과 계좌 정보를 입력

4. 설정 확인
```bash
python3 main.py doctor
```

5. 텔레그램 연결 테스트
```bash
python3 run_telegram_test.py
```

## 환경 점검 순서
자동 실행(`auto-run`, `liquidity-lab`) 전에 아래 순서로 환경이 정상인지 확인한다.
예시의 `<종목코드>`는 실제 보유했거나 테스트하고 싶은 종목으로 바꿔서 사용한다. 국내는 `005930` 같은 6자리 코드, 해외는 `NVDA` 같은 티커를 넣으면 된다.

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

해외주식 테스트 예시는 아래처럼 진행한다. `<종목코드>`, `<거래소코드>`, `<가격>`에는 실제 값(예: `NVDA`, `NASD`, `220.50`)을 넣는다.

```bash
# 1) 토큰 발급이 정상인지, 계좌 프로필이 올바른지 먼저 확인
python3 main.py auth-check
# 2) 지정 종목의 현재가/호가가 정상 조회되는지 확인 (거래소 코드 필수)
python3 main.py overseas-price-check <종목코드> --exchange <거래소코드>
# 3) 해외 계좌 잔고(보유 종목, 예수금)가 정상 조회되는지 확인
python3 main.py overseas-balance-check --exchange <거래소코드> --currency USD
# 4) 해당 가격에 실제로 몇 주까지 주문 가능한지(예수금 기준) 확인
python3 main.py overseas-orderable-check <종목코드> --exchange <거래소코드> --price <가격>
# 5) 매수 주문을 실제로 보내지 않고 요청 내용만 미리보기(기본 DRY_RUN)
python3 main.py overseas-order-test buy <종목코드> --exchange <거래소코드> --qty 1 --price <가격> --order-division 00
# 6) --execute로 실제 매수 주문 제출 (DRY_RUN=false 필수, 모의투자에서 먼저 시험)
DRY_RUN=false python3 main.py overseas-order-test buy <종목코드> --exchange <거래소코드> --qty 1 --price <가격> --order-division 00 --execute
# 7) 위에서 산 수량을 그대로 매도해 매수/매도 양쪽 경로 모두 확인
DRY_RUN=false python3 main.py overseas-order-test sell <종목코드> --exchange <거래소코드> --qty 1 --price <가격> --order-division 00 --execute
```

- 모의투자 미국주식 주문은 `00` 지정가 기준으로 테스트하는 쪽이 안전하다.
- KIS 모의투자는 미국주식 `매도`에서 제약이 있을 수 있어, 실제 응답 메시지를 함께 확인해야 한다.

## 운용 모드 비교: auto-run vs liquidity-lab
이 프로젝트는 같은 진입/청산 판단 로직(`momentum_policy.py`)을 공유하는 두 가지 실행 모드를 제공한다. 목적이 다르므로 상황에 맞게 선택한다.

| | `auto-run` | `liquidity-lab` |
|---|---|---|
| 감시 대상 | `auto_trade.symbol`에 지정한 **고정 1종목** | 국내는 `domestic_candidates`, 해외는 `TV scan` 또는 `/lab_relist` 목록에서 **매 사이클 자동 선정** |
| 적합한 상황 | 이미 매매하고 싶은 종목이 정해져 있을 때 | 그날 가장 활발한 종목을 자동으로 찾고 싶을 때 |
| 실행 | `python3 main.py` 또는 `python3 main.py auto-run` | `python3 main.py liquidity-lab` |
| 국내/해외 | `exchange_code` 설정에 따라 한 시장만 | 국내·해외 동시 운용 |
| 종목 변경 방법 | `config/fixed_config.json`의 `auto_trade.symbol` 수정 | 국내는 `liquidity_lab.domestic_candidates`, 해외는 TV 스캔 또는 `/lab_relist`로 조정 |

같은 종목을 두 모드 모두에서 보고 싶다면, `auto_trade.symbol`에 지정한 뒤 국내는 `domestic_candidates`에 넣고, 해외는 `/lab_relist`로 수동 고정하거나 TV 스캔으로 자동 선별되게 두면 된다. `auto-run`은 그 종목을 무조건 보고, `liquidity-lab`은 그날 활성 풀 안에서 더 활발한 종목을 우선 본다.

## 기본 자동 실행 (auto-run)
아무 옵션 없이 아래처럼 실행하면 된다.
```bash
python3 main.py
```

이 명령은 `config/fixed_config.json`의 `auto_trade` 정책을 읽어 다음을 수행한다.
- 대상: `auto_trade.symbol`에 지정한 고정 1종목
- 거래소: `auto_trade.exchange_code`
- 계정: 현재 `.env`의 `KIS_ENV`가 가리키는 프로필
- 기본 실행: `중지 전까지` 또는 `장 종료 전까지` 계속
- `max_actions_per_run=0`, `max_decision_cycles_per_run=0`이면 무제한 감시로 해석
- 액션 알림: 체결마다 텔레그램 전송
- 최종 알림: 수동 종료 또는 장 종료 시 손익 요약 텔레그램 전송
- 기본 예시 설정은 `NVDA` / `NASD` 이지만, 실제 운용 시 원하는 종목과 거래소 코드로 자유롭게 바꿔 쓰면 된다.

현재 기본 정책 라벨은 `FIXED_SYMBOL_MOMENTUM` 이다.
- 일봉 `20일선/60일선`과 1분봉 `3선/10선`은 `방향 필터`로만 사용한다.
- 실제 진입 트리거는 `분봉 거래량 비율 급증`, `직전 구간 고점 돌파`, `볼린저 상단 돌파`, `짧은 모멘텀 양수` 조합이다.
- 현재는 `직전 고점 완전 돌파`만 기다리지 않고, `고점의 98% 이상 근접 + 거래량 확장`이면 선제 진입을 허용한다.
- 현재 기본값은 `1분봉 + 25초 폴링` 기준이며, 최근 5분 안쪽의 짧은 흐름을 잡아 `수분 내 청산`을 목표로 한다.
- 보유 중에는 `ATR 기반 손절`, `모멘텀 약화`, `볼륨 페이드`, `트레일링 되돌림`, `시간초과`를 나눠서 판단한다.
- `매수 다음이 반드시 매도`가 아니며, 조건이 맞으면 분할매수와 분할매도를 모두 허용한다.
- 기준 턴마다 반드시 거래하지 않고, 조건이 약하면 `skip/pass`로 넘긴다.
- 모의투자에서 매도 잔고 반영이 늦을 경우, 실패 종료하지 않고 다음 사이클에 자동 재시도한다.

### 전략 핵심
- 진입은 `거래량 폭발 + 가격 돌파`가 메인이고, 이평선은 `상승 방향일 때만 롱 허용`하는 필터 역할만 한다.
- RSI는 더 이상 `68 이상이면 진입 금지` 같은 역방향 필터로 쓰지 않고, `85 초과`의 극단 과열만 차단한다.
- `trend_require_price_above_slow=false` 기본값으로, `3MA > 10MA` 방향만 살아 있으면 `price < 10MA`인 반등 초입도 진입 후보로 본다.
- 손절은 고정 퍼센트만 쓰지 않고, `ATR * 배수`와 `모멘텀 붕괴`를 함께 본다.
- 익절은 `부분 익절`, `트레일링`, `볼륨 감소`, `과열 RSI`를 함께 써서 한 번에 전량 정리하지 않도록 설계했다.
- `max_position_qty`, `allow_scale_in`, `allow_partial_exit`로 보유 수량을 고정하지 않고 가변적으로 조절한다.
- 기본값은 `slot sizing` 기반이다. `last_available_usd × slot_max_pct`를 슬롯 예산으로 잡고, 진입/추가매수 수량을 달러 금액 기준으로 역산한다.
- `poll_interval_sec=25`로 시세는 자주 감시하되, 무거운 차트 컨텍스트는 `1분봉 기준`으로 캐시해 호출 수를 억제한다.
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
- README에 적힌 종목 목록은 `고정 감시 목록`이 아니라 `스캔 후보 풀`이다.
- 실제 활성 감시는 매 사이클마다 후보 풀 전체를 다시 스캔해 `activity_score` 상위 종목만 추려서 동적으로 구성한다.
- `unified_watch_top_n` 밖으로 밀려나더라도, 이미 보유 중인 종목은 청산 판단을 위해 계속 감시한다.
- 흔히 말하는 `개잡주` 성격을 줄이기 위해, 현재 버전은 `저가주 + 얇은 거래대금/거래량 + 넓은 스프레드` 조합의 후보를 자동 제외한다.
- 국내 기본 제외 기준은 `3,000원 미만`, `당일 거래대금 500억 원 미만`, `최근 체결량 합계 10만 미만`, `스프레드 0.3% 초과`다.
- 해외 기본 제외 기준은 `5달러 미만`, `거래량 50만 미만`, `스프레드 0.3% 초과`, `가격×거래량 근사 거래대금 부족`이다.
- 현재 장이 열린 시장에서 `activity_score`가 높은 후보군을 먼저 뽑고, 그 안에서 `signal_score`가 가장 강한 종목을 우선 주문 대상으로 선택한다.
- 해외 후보군은 고정 목록 fallback 대신 TradingView Scanner 기반 동적 풀을 우선 사용한다.
- chart 기반 signal 계산은 `overseas_scan_top_n` 기준으로 우선 로드하며, 기본값은 `25`라 상위 25개와 보유 종목에만 signal 캐시를 붙인다.
- 보유 중인 해외 종목은 순위와 무관하게 signal 조회 대상에 항상 포함한다.
- 비보유 해외 종목이 chart signal 생성에 반복 실패하면 `overseas_signal_failure_threshold`(기본 `3`)회 이후 `overseas_signal_failure_cooldown_minutes`(기본 `180`분) 동안 제외한다.
- `watch_targets`와 보유 종목 청산 판단은 같은 사이클에 만든 `_signal_cache`를 재사용해 chart API를 다시 호출하지 않는다.
- 실제 해외 주문은 `activity_score`만으로 바로 넣지 않고, 선택된 후보가 전략 신호와 보조 필터를 함께 만족할 때만 진행한다.
- 최근 성과 기준으로 해외 `VWAP` 단독, `RSI` 단독, `VOL` 단독 진입은 기본 차단한다(`overseas_block_standalone_vwap=true`, `overseas_block_standalone_rsi=true`, `overseas_block_standalone_vol=true`). 해외에서는 `VWAP+RSI`, `VOL+RSI`처럼 보조 확인이 둘 이상 붙은 신호를 우선한다.
- 해외 신규 진입은 전략 신호가 있어도 `volume_ratio`가 `overseas_min_strategy_volume_ratio`(기본 `0.8`)보다 낮으면 `overseas_volume_floor`로 대기한다.
- `liquidity_lab`의 매수 수량은 기본적으로 슬롯 기반이다. `use_slot_sizing=true`이면 주문가능 금액에 `slot_entry_pct`를 곱한 예산 안에서 수량을 계산하고, 조회 실패 시에만 `*_test_order_qty` 고정 수량으로 폴백한다.
- 다만 해외 mock 포지션이 이미 있고 손절/익절 기준에 먼저 걸린 보유분이 있으면, 신규 매수보다 기존 보유 청산을 우선한다.
- 고정 손절/익절에 먼저 걸리지 않았더라도, 보유 종목이 `ATR 손절`, `모멘텀 약화`, `볼륨 페이드` 신호를 보이면 청산 후보로 올린다.
- 국내장이 열려 있으면 매 사이클마다 보유 포지션 청산 신호를 먼저 확인하고, 없으면 진입 조건을 충족한 종목의 신규 매수를 진행한다.
- 미국장이 열려 있으면 진입 조건을 충족한 해외 종목에 동시에 주문한다. 한 사이클에서 최대 `max_concurrent_overseas_orders`(기본 8)개까지 가능하다.
- 국내장이 열려 있으면 진입 조건을 충족한 국내 종목에도 동시에 주문한다. 한 사이클에서 최대 `max_concurrent_domestic_orders`(기본 8)개까지 가능하다.
- 국내+해외를 합친 총 동시 보유 종목 수는 `max_concurrent_total_positions`(기본 `10`)로 제한한다. 기본값 10은 슬롯 예산(`slot_entry_pct=0.10`, 슬롯당 자본의 약 10%) 기준으로 자본 100% 배치에 해당하는 구조적 상한이며, 시장별 한도와 별개로 두 시장 합산 노출을 함께 묶는다. `0`이면 합산 한도를 끄고 시장별 한도만 적용한다. 합산 한도로 해외 신규 진입이 막히면 skip 사유가 `total_position_cap_reached`로 기록된다.
- 시장별/합산 주문 한도는 KIS API 트래픽 제약 때문이 아니라 순수 리스크 노출 관리용 값이다. 현재는 전략·시스템 수정 단계라 단기 성과를 근거로 한도를 올리거나 내리는 조정은 하지 않는 것을 원칙으로 한다.
- 자동 사이클에서는 더 이상 국내 `paper-run` 25초 검증을 끼워 넣지 않는다. 수동 검증이 필요하면 텔레그램 `/lab_paper_test <종목코드>`를 사용한다.
- 해외 고정 손절(`overseas_stop_loss_pct`)은 일시적 wick(단일 체결 급락 후 즉시 회복)에 속지 않도록 확인 단계를 거친다(`overseas_stop_loss_confirm_enabled=true`). 손절 기준을 갓 넘긴 첫 관측이고 매도 거래량 확인이 안 되면 다음 사이클까지 한 번 대기(`stop_loss_confirm_wait`)하고, 다음 사이클에도 손절권이면 그때 손절한다. 단, ①손실이 `overseas_stop_loss_hard_multiplier`(기본 2.0)배를 넘는 깊은 손실이거나 ②현재 분봉 거래량이 평소 대비 `overseas_stop_loss_volume_confirm_ratio`(기본 1.5)배 이상 급증한 음봉(실제 매도세 확인)이면 대기 없이 즉시 손절한다. 손실이 손절권 위로 회복되면 대기 기록은 초기화된다.
- 위 손절 확인과 별개로, 기준가 대비 `overseas_exit_price_shock_pct`(기본 20%) 이상 튀는 극단적 가격은 데이터 오류 가능성이 있어 기존처럼 다음 사이클 재확인 후에만 청산 판단에 사용한다. 두 장치는 계층이 다르다: 손절 확인은 현실적인 1~2% 급락 구간의 회복 가능성 판단, 쇼크 가드는 이상 호가/오염 데이터 차단.
- 기본 매수와 일반 익절/시간청산은 지정가로 제출한다. 다만 `손절`, `ATR 하드스탑`, `모멘텀 손절`, `추세 이탈 손절`, `손실 상태 시간청산` 같은 보호성 청산은 체결력을 우선한다.
- 보호성 청산 주문은 국내는 시장가(`ORD_DVSN=01`, 제출가 0), 해외 실계좌는 시장가, 해외 모의투자는 KIS 안정성을 위해 기준 호가의 공격지정가(`ORD_DVSN=00`)로 제출한다.
- 손익 계산과 텔레그램 표시는 실제 제출가 0이 아니라 청산 판단 당시의 기준 호가(`reference_price`)를 사용한다. 내부 broker audit에는 `order_kind`, `order_division`, `requested_price`, `reference_price`를 함께 기록한다.
- **주문거부 서킷브레이커**: 매도 주문거부는 시장별로 개별 종목 쿨다운(국내 10분/해외 20분)을 걸지만, 매수 주문거부에는 원래 아무 백오프가 없어 같은 오류가 나는 동안 사이클마다 계속 재시도했다. 이제 시장×방향(`domestic:buy`, `overseas:sell` 등) 기준으로 최근 `order_reject_window_minutes`(기본 15분) 안에 `order_reject_threshold`(기본 5)회 이상 주문거부가 쌓이면 그 시장/방향의 신규 주문을 `order_reject_cooldown_minutes`(기본 30분) 동안 중단하고, KIS가 반환한 실제 오류 메시지를 담아 텔레그램으로 즉시 알린다. `/lab_guard`에 `주문거부차단=` 줄로 현재 차단 대상과 누적 건수를 보여주고, `/lab_cb_reset`으로 즉시 해제할 수 있다. `order_reject_threshold=0`이면 기능을 끈다.

현재 기본 후보군과 개잡주 필터 기준은 `config/fixed_config.json`의 `liquidity_lab` 섹션에서 조정할 수 있다.
- 국내: `005930`, `000660`, `035420`, `419050`, `023410`, `010170`, `034940`
- 해외: 기본 고정 후보는 비워 두고, TradingView Scanner 결과 또는 `/lab_relist` 수동 목록을 사용한다.

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

서비스로 올려두면 `liquidity-lab` 테스트가 끝난 뒤에도 컨트롤러는 계속 살아 있고, 텔레그램 명령만 보내면 다시 수행할 수 있다. 서비스가 시작되면 텔레그램 `setMyCommands`도 함께 등록되어, 채팅창에서 `/`를 입력했을 때 자동완성 목록과 메뉴 버튼 명령 목록이 보이도록 설정된다.

지원 명령:
- `/lab_start`: 즉시 루프 시작
- `/lab_pause`: 현재 사이클은 마무리하고 일시정지
- `/lab_resume`: 일시정지 상태에서 재개
- `/lab_stop`: 현재 사이클 취소 요청 후 정지. 그 시점까지의 누적 거래/손익 요약을 텔레그램으로 전송하고 DB에 기록
- `/lab_terminate`: 현재 lab 실행을 강제 종료하고 대기 상태로 복귀. 그 시점까지의 누적 거래/손익 요약을 텔레그램으로 전송하고 DB에 기록
- `/lab_service_restart`: `kinvest-telegram-control.service` 자체를 재시작
- `/lab_status`: 현재 상태, 가상 노출, 최근 반복 매도장애 요약 조회
- `/lab_watchlist`: 현재 감시중인 종목 목록과 `20d/60d`, `5/20` 이평 관계, `vr/mom` 기반 짧은 상태 요약 조회
- `/lab_portfolio`: 실제 계좌 보유, 통합 가상보유, 정산 대기 매도, 누적 성과 조회
- `/lab_log`: `/lab_start` 이후 세션 기준 실거래/가상거래 손익 요약 조회
- `/lab_performance [시간]`: 최근 N시간(기본 24시간)의 실주문접수 `SELL_REAL`만 전략별로 집계. 감시 신호 `BUY/SELL/HOLD`는 제외
- `/lab_report compare <YYYY-MM-DD|YYYY-MM-DDTHH:MM>`: 기준일/시각 전후 전략별 실주문접수 성과 비교
- `/lab_report wait [시간]`: 최근 N시간(기본 72시간)의 `WAIT` 병목을 시장·전략·사유별로 요약
- `/lab_guard`: 최근 성과 기준 전략 가드 상태 조회. `차단/감시/참고`와 고정차단(`해외 VWAP단독`, `해외 RSI단독`, `해외 VOL단독`)을 함께 표시한다
- `/lab_orders`: 최근 주문 접수/취소/거부 기록, KIS 실시간 미체결 주문, 접수 후 체결확정 추적 필요 주문 조회
- `/lab_cancel_stale_domestic`: 30분 이상 국내 미체결 취소 대상 확인
- `/lab_cancel_stale_domestic_confirm`: 확인된 국내 장기 미체결 취소 실행(메뉴에는 숨김)
- `/lab_cancel_stale_overseas`: 30분 이상 해외 미체결 취소 대상 확인
- `/lab_cancel_stale_overseas_confirm`: 확인된 해외 장기 미체결 취소 실행(메뉴에는 숨김)
- `/lab_trim_virtual`: 해외 가상보유가 `max_concurrent_overseas_orders` 한도를 초과했을 때, 손실이 크고 오래된 초과분 정리 후보를 실시간 시세 기준으로 미리보기
- `/lab_trim_virtual_confirm`: `/lab_trim_virtual`이 제시한 초과분 정리 실행(메뉴에는 숨김)
- `/lab_reset`: 가상거래 전체를 백업 후 초기화(현재 가상보유/노출/한도초과 요약을 먼저 보여준 뒤 확인 요청)
- `/lab_reset_confirm`: `/lab_reset`이 제시한 초기화 실행(메뉴에는 숨김)
- `/lab_relist`: 해외 감시 풀을 수동 종목 목록으로 교체(TV 스캔 대신 특정 종목만 보고 싶을 때)
- `/lab_relist_schedule`: 해외 relist 관련 알림 시간 설정
- `/lab_cb_reset`: 연속손절/일일손실 서킷브레이커 및 주문거부 서킷브레이커 강제 해제
- `/lab_gitlog`: 당일 거래/이벤트/주문/텔레그램/API 호출 로그를 CSV 5종으로 정리해 GitHub 저장소에 업로드
- `/lab_paper_test <종목코드>`: 지정 국내 종목으로 수동 paper test 실행
- `/lab_help`: 명령 목록 조회

### `/lab_gitlog`가 업로드하는 5종 로그
`/lab_gitlog`는 그날(KST 기준) 발생한 아래 5개 CSV를 `logs/<종류>/YYYYMMDD_*.csv`로 업로드한다.
매매 요청부터 응답, 텔레그램 알림까지 하나의 사건을 여러 로그에서 서로 대조해 분석할 수 있도록
전량을 그대로 내보낸다.

| 파일 | 내용 |
|------|------|
| `trades` | `cycle_log` 전체 (BUY/SELL/HOLD/WAIT/SKIP 등 그날의 모든 판단, 실거래/가상거래 구분 없이 전량) |
| `events` | `event_log` 전체 (서킷브레이커 발동/해제, 신호실패, 풀 갱신 등 시스템 이벤트) |
| `orders` | `broker_order_events` 전체 (실제 KIS에 제출된 모든 주문 요청과 응답 — 체결/거부/취소/가상기록 사유 포함) |
| `telegram` | 그날 수신한 `/lab_*` 명령과 발송한 모든 텔레그램 알림(성공/실패 여부 포함) |
| `api_calls` | 그날의 모든 KIS API 호출 요약(TR_ID, 경로, 성공여부, 응답코드/메시지, 소요시간) |

**보안 주의**: 이 저장소(`tagynedlrb/kinvest_trade`)는 public이다. 위 5종 로그는 계좌번호(CANO),
APPKEY/APPSECRET, HTS ID를 **절대 포함하지 않도록** 설계했다 — `api_call_log`는 요청 바디 대신
TR_ID/경로/응답코드/메시지 요약만 저장하고, `broker_order_events.payload_json`은 KIS 응답의
주문번호(ODNO)·메시지만 담아(계좌 정보는 KIS 응답 자체에도 없음) 애초에 계좌 식별 정보가 로그에
쌓이지 않는다. 새로운 로그 필드를 추가할 때는 항상 이 원칙(요청 바디·자격증명 원문을 저장하지
않음)을 지켜야 한다.

### `/lab_portfolio` 보유상태 불일치 감지
`/lab_portfolio`는 실시간 KIS 잔고를 별도 임시 클라이언트로 재조회해서 보여주는데, 이 재조회가
조용히 실패하거나 일부만 반영되면(예외는 로그에만 남고 화면에는 안 보임) 실제로는 보유 중인
종목이 "보유종목=없음"처럼 잘못 표시될 수 있었다. 이제 실시간 조회 결과와 매매 루프 자신의
캐시(`lab_symbol_state.has_position=1`)를 대조해서, 루프는 보유중으로 기록했는데 화면에는
안 보이는 종목이 있으면 `─── 보유상태 불일치 ───` 섹션에 `내부기록=N주 조회결과=없음`으로
표시한다. 가상보유 전용 종목은 실보유 목록에 없는 게 정상이라 이 검사에서 제외한다.

### 손익 확인 (`/lab_log`)
`/lab_log`는 `/lab_start` 이후 발생한 모든 거래의 손익을 집계해 표시한다.

- 모의투자: 실거래(KIS 모의서버 체결) + 가상거래(virtual, 거래불가 세션 대체) 손익을 함께 표시
- 실거래: 실거래 손익만 표시하고 가상거래는 제외
- 표시 항목: 거래 건수, 승률, 해외 USD 손익, KRW 환산 손익, 시장별 세부 통계

수수료, 세금, 환율 손익은 반영되지 않은 매매 차익 기준 추정값이며, 정확한 실현 손익은 KIS 앱에서 확인하는 것이 안전하다.

메뉴 메모:
- `/lab_paper_test`는 텔레그램 메뉴에서 누르면 종목코드 없이 들어오므로, 실제 실행할 때는 `/lab_paper_test 005930`처럼 직접 종목코드를 덧붙여 입력해야 한다.

동작 메모:
- `중지(stop)`는 루프를 멈추지만 컨트롤러 프로세스는 살아 있다.
- `종료(terminate)`도 컨트롤러 서비스는 유지하고, lab 실행만 강제로 끝낸 뒤 명령 대기 상태로 돌아간다.
- `stop`/`terminate` 요약은 `telegram_control_sessions` 테이블에도 저장되어 다음 전략 개선 때 누적 성과를 되짚는 데 사용한다.
- `stop`/`terminate` 요약은 `종목별 buy/sell 횟수`와 함께 `/lab_log`와 동일한 세션 손익 요약을 함께 보여준다.
- 장이 닫혀도 `no_supported_market_open`만으로 자동 정지하지 않는다. 서비스는 계속 살아 있고, 장이 다시 열리면 자동으로 감시/거래를 재개한다.
- 다음 자동 실행 간격은 장 상태와 오류 횟수에 따라 동적으로 결정된다.
- 거래 가능 세션은 `20초`, 미장 pre/after는 `30초`, 양쪽 장이 모두 닫혔고 다음 장이 멀면 `120초`까지 늘려 불필요한 호출을 줄인다.
- 다음 실행 시점은 `이전 사이클 종료 후 추가 대기`가 아니라 `이전 사이클 시작 시점 기준`으로 계산해 감시 간격이 불필요하게 늘어지지 않도록 했다.
- 텔레그램 long polling 시간은 `notifications.telegram_command_poll_timeout_sec`으로 조절한다.
- 서비스 로그는 `journalctl --user -u kinvest-telegram-control.service -f`로 확인할 수 있다.
- `WAIT` 상태는 더 이상 텔레그램으로 매 사이클 전송하지 않는다. 텔레그램 알림은 실제 `매수/매도 제출` 또는 `주문 오류` 중심으로만 보낸다.
- `/lab_status`에는 현재 장 상태, 다음 루프 간격, 연속 오류 횟수, 가상 노출, 최근 12시간 반복 매도장애(`매도가능0`, `주문거부`)가 함께 표시된다.
- 장 상태가 `krx_open`, `us_regular`, `both_closed` 등으로 바뀌면 텔레그램에 자동 알림을 보낸다.
- 현재 기본 해외 감시는 `TV scan -> dynamic pool`, `overseas_scan_top_n=25`, `loop_interval_sec=25` 기준이다. TV 스캔 실패 시에는 자동 fallback 대신 relist 요청 알림을 보내고, 보유 종목은 풀 상태와 무관하게 계속 감시한다.
- 자동매매 SELL 알림은 `종목 / 매수·매도 / 가격 / 수량 / RSI·거래량 / 손익 / 보유시간` 위주로 짧게 보낸다.
- 재시작 후 평균매입가 복구가 실패하면 `매입가=알수없음`, `수익률=알수없음`으로 명확히 표기한다.
- `liquidity_lab`가 직접 해외 매도를 실행한 경우에도 `[KIS][LAB_SELL]` 텔레그램 알림이 별도로 전송된다.
- `liquidity_lab`는 이제 국내 보유 포지션도 감시 목록에 포함해 손절/익절 신호가 나오면 실제 국내 매도 경로로 연결된다.
- `/lab_portfolio`는 국내/해외 실제 보유 종목과 가상 체결 반영 통합 보유, 정산 대기 매도, 누적 실현손익을 함께 보여준다.
- `/lab_performance`는 전략 평가용이다. `cycle_log`의 감시 신호 행(`BUY`, `SELL`, `HOLD`)을 제외하고 실주문접수 매도 행(`SELL_REAL`)만 집계한다. 체결 확정 여부는 MTS/잔고 기준으로 확인한다.
- `/lab_guard`는 `strategy_guard_lookback_hours` 기간의 실주문접수 `SELL_REAL`을 기준으로 전략별 평균 순손익을 보여준다. 차단 기준은 `strategy_guard_min_trades`와 `strategy_guard_max_avg_net_pnl_pct`를 따른다.
- `/lab_orders`는 내부 주문 이벤트와 KIS 실시간 미체결 주문을 함께 보여준다. 내부 `SUBMITTED` 기록은 체결 확정이 아니므로, `접수 후 체결확정 추적 필요` 섹션에서 `확인필요=MTS/잔고`로 따로 표시한다. live 미체결 조회가 성공하면 `브로커상태=미체결` 또는 `브로커상태=미체결목록없음`도 함께 표시한다. 국내 장기 미체결은 장외 시간에 `취소가능=국내장중`으로 표시되며, 봇이 접수한 장기 미체결 국내 주문은 다음 국내 정규장에 자동 취소를 재시도한다. 해외도 봇이 접수한 장기 미체결 주문만 미국 주문 가능 세션에 자동 취소를 재시도한다.

## 거래 시간 정책
이 프로그램은 국내(KRX)와 해외(미국) 시장을 동시에 감시하며, 각 시장의 세션 상태에 따라 거래 가능 여부가 자동으로 결정된다. 판단 로직은 `src/kinvest_trade/market_sessions.py`에 구현되어 있다.

### KRX(국내) 거래 시간

| 구분 | 시간 (KST) | 비고 |
|------|-----------|------|
| 정규장 | 09:00 ~ 15:30 | 평일만, 모의/실전 동일 |

국내는 모의투자와 실전투자의 거래 가능 시간 차이가 없다.

### 미국(해외) 거래 시간 (KST 기준)
미국 동부시간 기준 일광절약시간(서머타임, 3월~11월 둘째 일요일) 적용 여부에 따라 KST 시간이 달라진다.

**일광절약시간 적용 시 (3월 둘째 일요일 ~ 11월 첫째 일요일)**

| 세션 | 시간 (KST) | 의미 |
|------|-----------|------|
| 데이타임(주간거래) | 10:00 ~ 17:00 | 미국 정규장 개장 전 대체거래소(ATS) 거래 |
| 프리마켓 | 17:00 ~ 22:30 | 정규장 개장 준비 |
| 정규장 | 22:30 ~ 익일 05:00 | 미국 나스닥/NYSE 정규 거래시간 |
| 애프터마켓 | 05:00 ~ 07:00 | 정규장 마감 후 |

**일광절약시간 미적용 시 (11월 첫째 일요일 ~ 3월 둘째 일요일)**

| 세션 | 시간 (KST) | 의미 |
|------|-----------|------|
| 데이타임(주간거래) | 10:00 ~ 18:00 | 미국 정규장 개장 전 대체거래소(ATS) 거래 |
| 프리마켓 | 18:00 ~ 23:30 | 정규장 개장 준비 |
| 정규장 | 23:30 ~ 익일 06:00 | 미국 나스닥/NYSE 정규 거래시간 |
| 애프터마켓 | 06:00 ~ 07:00 | 정규장 마감 후 |

### 모의투자 vs 실전투자 거래 가능 세션
**이 차이가 "거래불가 세션" 메시지의 직접적인 원인이다.**

| 환경 | 거래 가능한 미국 세션 |
|------|----------------------|
| 모의투자 (vps) | **정규장(regular)만** |
| 실전투자 (prod) | 데이타임, 프리마켓, 정규장, 애프터마켓 전부 |

모의투자 계좌는 미국 정규장 외 시간대(데이타임/프리마켓/애프터마켓) 주문을 KIS 서버 단계에서 거부한다. 이 프로그램은 해당 시간대에 실제 시장이 열려 있으면 해당 거부 주문을 `(virtual)`로 가상 체결해 별도 포트폴리오에 기록한다. 따라서 미국 extended session에서는 실주문 대신 `[KIS][VIRTUAL_TRADE]` 알림이 올 수 있으며, 이는 버그가 아니라 KIS 모의투자 제한을 우회해 전략 성능을 검증하기 위한 정상 동작이다.

실전투자로 전환하면 데이타임~애프터마켓까지 거래가 가능해지므로 이 제한이 사라진다. 전환 방법은 [실계좌 전환 방법](#실계좌-전환-방법) 섹션을 참고한다.

### 가상(virtual) 거래
모의투자 환경에서 미국 시장이 열려 있지만 주문이 거부되는 세션(데이타임/프리마켓/애프터마켓)에는 실제 브로커 잔고와 분리된 `virtual_positions`, `virtual_orders` 테이블에 가상 체결을 기록한다. 실제 보유분을 거래불가 세션에 먼저 가상 매도한 경우에는 `virtual_sell_pending`에 정산 대기 수량이 음수 성격으로 따로 쌓인다. 이 포트폴리오는 실제 `liquidity_lab`의 진입/청산 신호를 그대로 따르지만, `get_overseas_balance` 등 실제 잔고와는 섞이지 않는다. 거래 가능 시간이 되면 정산 대기 매도는 실제 매도로 맞춰지고 `[KIS][VIRTUAL_SETTLED]` 알림이 전송된다. 텔레그램 알림은 종목명 뒤에 `(virtual)`이 붙고, 누적 성과와 정산 대기 상태는 `/lab_portfolio` 명령으로 확인할 수 있다.
- 이 가상매도 전환은 매수/매도 양쪽 모두에서 동작한다: 매수는 세션 제한으로 거부되면
  즉시 가상매수로, 실보유 종목의 매도도 세션 제한(`session_not_orderable_in_profile`)에
  걸리면 실전투자 기준으로 지금 거래 가능한 시간대인지 확인해 가상매도로 전환한다. 실전투자
  기준으로도 완전히 장이 닫혀 있을 때만(주말/공휴일 등) 그냥 대기한다.

### 시장이 모두 닫혀 있을 때
국내장과 미국장(애프터마켓 포함)이 모두 닫혀 있는 시간대(KST 07:00~09:00, 15:30~17:00 부근)에는 프로그램이 API 호출 없이 대기 상태로 유지된다. 다음 거래 가능 세션이 임박하면(30분 이내) 감시 주기가 짧아지고, 그렇지 않으면 길어진다(`determine_loop_interval_sec` 참고). 이때 KRX/NYSE 휴장일은 다음 거래 가능 세션 계산에서 건너뛰며, 미국장은 KIS의 한국시간 기반 주간/프리/정규/애프터 세션 날짜에 맞춰 휴장일을 판단한다.

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

## 실시간 감시 (보조 도구)
`run_watch.py`는 자동매매가 아니라 콘솔에서 지표를 보는 보조 도구다.

```bash
python3 run_watch.py
```

한 번만 시험:
```bash
KIS_WATCH_MAX_CYCLES=1 python3 run_watch.py
```

## 주요 명령
`<종목코드>`, `<거래소코드>`, `<가격>`, `<국내종목코드>`에는 실제 값(예: `NVDA`, `NASD`, `220.50`, `005930`)을 직접 채워 넣는다.

```bash
# 기본 실행: auto_trade.symbol에 고정된 단일 종목 자동 매매 (인자 없으면 이 모드)
python3 main.py
# 위와 동일하게 auto-run을 명시적으로 지정
python3 main.py auto-run
# 국내/해외 후보군을 스캔해 매 사이클 가장 활발한 종목을 자동 선정해 테스트
python3 main.py liquidity-lab
# 텔레그램 봇 명령으로 liquidity-lab를 원격 제어하는 상주 컨트롤러 실행
python3 main.py telegram-control
# 현재 설정값과 안전장치(DRY_RUN, LIVE_TRADING_ENABLED 등) 상태 출력
python3 main.py doctor
# 토큰 발급 + 인증 상태 확인
python3 main.py auth-check
# 국내 계좌 잔고 조회
python3 main.py balance-check
# 해외 종목 현재가/호가 조회 (예시 종목코드는 직접 보유/관심 종목으로 교체)
python3 main.py overseas-price-check <종목코드> --exchange <거래소코드>
# 해외 계좌 잔고 조회
python3 main.py overseas-balance-check --exchange <거래소코드> --currency USD
# 해당 가격에서 주문 가능 수량 확인
python3 main.py overseas-orderable-check <종목코드> --exchange <거래소코드> --price <가격>
# 해외 매수 주문 미리보기 (DRY_RUN 기본값, 실제 제출 안 함)
python3 main.py overseas-order-test buy <종목코드> --exchange <거래소코드> --qty 1 --price <가격> --order-division 00
# 해외 매수 주문 실제 제출 (--execute + DRY_RUN=false 둘 다 필요)
python3 main.py overseas-order-test buy <종목코드> --exchange <거래소코드> --qty 1 --price <가격> --order-division 00 --execute
# 국내 종목 1분봉 기준 지표(RSI, 이동평균 등) 조회
python3 main.py indicator-check <국내종목코드> --timeframe minute
# 국내 종목 일봉 기준 지표 조회
python3 main.py indicator-check <국내종목코드> --timeframe daily
# 국내 매수 가능 수량 조회
python3 main.py orderable-check <국내종목코드> --price <가격> --order-division 01
# 국내 매수 주문 미리보기
python3 main.py order-test buy <국내종목코드> --qty 1 --price <가격> --order-division 00
# 국내 매수 주문 실제 제출
python3 main.py order-test buy <국내종목코드> --qty 1 --price <가격> --order-division 00 --execute
# 국내 종목 실시간 지표 감시 콘솔 (별도 독립 스크립트, cli.py를 거치지 않음)
python3 run_watch.py
# 실제 주문 없이 페이퍼 트레이딩 20회 반복(15초 간격) 실행
python3 main.py paper-run --iterations 20 --interval-sec 15
# 가장 최근 paper-run 결과 요약 출력
python3 main.py paper-report
# 텔레그램 알림이 정상 발송되는지 테스트
python3 run_telegram_test.py
```

## 참고한 공식 자료
- KIS API 포털: https://apiportal.koreainvestment.com/
- KIS 공식 샘플 저장소: https://github.com/koreainvestment/open-trading-api
