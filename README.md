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
- `src/kinvest_trade/cli.py`: `liquidity-lab` 명령으로 국내/해외 고유동성 후보를 스캔하고 조건 충족 시 즉시 주문 테스트까지 수행할 수 있다.
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
- 현재는 `직전 고점 완전 돌파`만 기다리지 않고, `고점의 98% 이상 근접 + 거래량 확장`이면 선제 진입을 허용한다.
- 현재 기본값은 `1분봉 + 10초 폴링` 기준이며, 최근 5분 안쪽의 짧은 흐름을 잡아 `수분 내 청산`을 목표로 한다.
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
- 해외 후보군은 고정 1종목만 보는 대신, 현재는 `69개` 전체 후보를 `20초`마다 전부 quote 스캔한다.
- chart 기반 signal 계산은 `overseas_scan_top_n` 기준으로 우선 로드하며, 기본값은 `15`라 상위 15개와 보유 종목에만 signal 캐시를 붙인다.
- 보유 중인 해외 종목은 순위와 무관하게 signal 조회 대상에 항상 포함한다.
- `watch_targets`와 보유 종목 청산 판단은 같은 사이클에 만든 `_signal_cache`를 재사용해 chart API를 다시 호출하지 않는다.
- 실제 해외 주문은 `activity_score`만으로 바로 넣지 않고, 선택된 후보가 `거래량 스파이크 + 돌파 + 추세 필터`를 동시에 만족할 때만 진행한다.
- 다만 해외 mock 포지션이 이미 있고 손절/익절 기준에 먼저 걸린 보유분이 있으면, 신규 매수보다 기존 보유 청산을 우선한다.
- 고정 손절/익절에 먼저 걸리지 않았더라도, 보유 종목이 `ATR 손절`, `모멘텀 약화`, `볼륨 페이드` 신호를 보이면 청산 후보로 올린다.
- 국내 장이 열려 있고 `primary_target`이 국내 종목이면 보유 포지션 청산 신호를 먼저 확인하고, 없으면 신규 mock 주문 테스트를 즉시 진행한다.
- 미국장이 열려 있고 `primary_target`이 해외 종목이면 그 1개 종목만 mock 주문 테스트 대상으로 사용한다.
- 자동 사이클에서는 더 이상 국내 `paper-run` 25초 검증을 끼워 넣지 않는다. 수동 검증이 필요하면 텔레그램 `/lab_paper_test <종목코드>`를 사용한다.

현재 기본 후보군과 개잡주 필터 기준은 `config/fixed_config.json`의 `liquidity_lab` 섹션에서 조정할 수 있다.
- 국내: `005930`, `000660`, `035420`, `419050`, `023410`, `010170`, `034940`
- 해외: `NVDA`, `AMD`, `INTC`, `MU`, `AVGO`, `ARM`, `SMCI`, `QCOM`, `TXN`, `AAPL`, `MSFT`, `AMZN`, `META`, `GOOGL`, `TSLA`, `NFLX`, `ORCL`, `CRM`, `ADBE`, `PLTR`, `COIN`, `MSTR`, `PYPL`, `HOOD`, `SOFI`, `AFRM`, `SNAP`, `RBLX`, `DKNG`, `UBER`, `LYFT`, `SPOT`, `ROKU`, `SHOP`, `MELI`, `AAL`, `JPM`, `BAC`, `C`, `WFC`, `GS`, `XOM`, `CVX`, `NEE`, `F`, `GM`, `GE`, `BA`, `CAT`, `DE`, `UPS`, `FDX`, `NKE`, `DIS`, `WMT`, `HD`, `T`, `VZ`, `PFE`, `JNJ`, `LLY`, `MRNA`, `ABBV`, `UNH`, `V`, `MA`, `KO`, `PEP`, `SQ`

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
서비스가 시작되면 텔레그램 `setMyCommands`도 함께 등록되어, 채팅창에서 `/`를 입력했을 때 자동완성 목록과 메뉴 버튼 명령 목록이 보이도록 설정된다.

지원 명령:
- `/lab_start`: 즉시 루프 시작
- `/lab_pause`: 현재 사이클은 마무리하고 일시정지
- `/lab_resume`: 일시정지 상태에서 재개
- `/lab_stop`: 현재 사이클 취소 요청 후 정지. 그 시점까지의 누적 거래/손익 요약을 텔레그램으로 전송하고 DB에 기록
- `/lab_terminate`: 현재 lab 실행을 강제 종료하고 대기 상태로 복귀. 그 시점까지의 누적 거래/손익 요약을 텔레그램으로 전송하고 DB에 기록
- `/lab_service_restart`: `kinvest-telegram-control.service` 자체를 재시작
- `/lab_status`: 현재 상태 조회
- `/lab_watchlist`: 현재 감시중인 종목 목록과 `20d/60d`, `5/20` 이평 관계, `vr/mom` 기반 짧은 상태 요약 조회
- `/lab_positions`: 현재 보유 포지션과 미실현 손익 조회
- `/lab_virtual`: 거래불가 세션에서 가상 체결된 별도 포트폴리오와 누적 성과 조회
- `/lab_paper_test <종목코드>`: 지정 국내 종목으로 수동 paper test 실행
- `/lab_help`: 명령 목록 조회

메뉴 메모:
- `/lab_paper_test`는 텔레그램 메뉴에서 누르면 종목코드 없이 들어오므로, 실제 실행할 때는 `/lab_paper_test 005930`처럼 직접 종목코드를 덧붙여 입력해야 한다.

동작 메모:
- `중지(stop)`는 루프를 멈추지만 컨트롤러 프로세스는 살아 있다.
- `종료(terminate)`도 컨트롤러 서비스는 유지하고, lab 실행만 강제로 끝낸 뒤 명령 대기 상태로 돌아간다.
- `stop`/`terminate` 요약은 `telegram_control_sessions` 테이블에도 저장되어 다음 전략 개선 때 누적 성과를 되짚는 데 사용한다.
- `stop`/`terminate` 요약은 `종목별 buy/sell 횟수`, `domestic paper 실현손익`, `해외 청산 추정손익`까지 함께 묶어 짧게 보여준다.
- 장이 닫혀도 `no_supported_market_open`만으로 자동 정지하지 않는다. 서비스는 계속 살아 있고, 장이 다시 열리면 자동으로 감시/거래를 재개한다.
- 다음 자동 실행 간격은 장 상태와 오류 횟수에 따라 동적으로 결정된다.
- 거래 가능 세션은 `20초`, 미장 pre/after는 `30초`, 양쪽 장이 모두 닫혔고 다음 장이 멀면 `120초`까지 늘려 불필요한 호출을 줄인다.
- 다음 실행 시점은 `이전 사이클 종료 후 추가 대기`가 아니라 `이전 사이클 시작 시점 기준`으로 계산해 감시 간격이 불필요하게 늘어지지 않도록 했다.
- 텔레그램 long polling 시간은 `notifications.telegram_command_poll_timeout_sec`으로 조절한다.
- 서비스 로그는 `journalctl --user -u kinvest-telegram-control.service -f`로 확인할 수 있다.
- `WAIT` 상태는 더 이상 텔레그램으로 매 사이클 전송하지 않는다. 텔레그램 알림은 실제 `매수/매도 제출` 또는 `주문 오류` 중심으로만 보낸다.
- `/lab_status`에는 현재 장 상태, 다음 루프 간격, 연속 오류 횟수가 함께 표시된다.
- 장 상태가 `krx_open`, `us_regular`, `both_closed` 등으로 바뀌면 텔레그램에 자동 알림을 보낸다.
- 현재 기본 해외 감시는 `overseas_candidates=69`, `overseas_scan_top_n=15`, `loop_interval_sec=20` 기준이다. 매 사이클 전체 후보를 quote 스캔하고, signal은 상위 15개와 보유 종목에만 계산한다.
- 자동매매 SELL 알림은 `종목 / 매수·매도 / 가격 / 수량 / RSI·거래량 / 손익 / 보유시간` 위주로 짧게 보낸다.
- 재시작 후 평균매입가 복구가 실패하면 `매입가=알수없음`, `수익률=알수없음`으로 명확히 표기한다.
- `liquidity_lab`가 직접 해외 매도를 실행한 경우에도 `[KIS][LAB_SELL]` 텔레그램 알림이 별도로 전송된다.
- `liquidity_lab`는 이제 국내 보유 포지션도 감시 목록에 포함해 손절/익절 신호가 나오면 실제 국내 매도 경로로 연결된다.
- `/lab_positions`는 국내/해외 보유 종목을 함께 보여주고, `/lab_watchlist`는 시장·상태·이평·메모·가격 한 줄 형식으로 요약한다.
- `/lab_virtual`는 미국 거래불가 세션에서 `(virtual)`로 체결된 별도 포트폴리오와 누적 실현손익만 따로 보여준다.

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

## 거래 시간 정책

이 프로그램은 국내(KRX)와 해외(미국) 시장을 동시에 감시하며,
각 시장의 세션 상태에 따라 거래 가능 여부가 자동으로 결정된다.
판단 로직은 `src/kinvest_trade/market_sessions.py`에 구현되어 있다.

### KRX(국내) 거래 시간

| 구분 | 시간 (KST) | 비고 |
|------|-----------|------|
| 정규장 | 09:00 ~ 15:30 | 평일만, 모의/실전 동일 |

국내는 모의투자와 실전투자의 거래 가능 시간 차이가 없다.

### 미국(해외) 거래 시간 (KST 기준)

미국 동부시간 기준 일광절약시간(서머타임, 3월~11월 둘째 일요일)
적용 여부에 따라 KST 시간이 달라진다.

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

모의투자 계좌는 미국 정규장 외 시간대(데이타임/프리마켓/애프터마켓)
주문을 KIS 서버 단계에서 거부한다. 이 프로그램은 해당 시간대에
실제 시장이 열려 있으면 해당 거부 주문을 `(virtual)`로 가상 체결해
별도 포트폴리오에 기록한다. 따라서 미국 extended session에서는
실주문 대신 `[KIS][VIRTUAL_TRADE]` 알림이 올 수 있으며, 이는 버그가
아니라 KIS 모의투자 제한을 우회해 전략 성능을 검증하기 위한 정상 동작이다.

실전투자로 전환하면 데이타임~애프터마켓까지 거래가 가능해지므로
이 제한이 사라진다. 전환 방법은 [실계좌 전환 방법](#실계좌-전환-방법)
섹션을 참고한다.

### 가상(virtual) 거래

모의투자 환경에서 미국 시장이 열려 있지만 주문이 거부되는 세션
(데이타임/프리마켓/애프터마켓)에는 실제 브로커 잔고와 분리된
`virtual_positions`, `virtual_orders` 테이블에 가상 체결을 기록한다.
이 포트폴리오는 실제 `liquidity_lab`의 진입/청산 신호를 그대로 따르지만,
`get_overseas_balance` 등 실제 잔고와는 섞이지 않는다. 텔레그램 알림은
종목명 뒤에 `(virtual)`이 붙고, 누적 성과는 `/lab_virtual` 명령으로
확인할 수 있다.

### 시장이 모두 닫혀 있을 때

국내장과 미국장(애프터마켓 포함)이 모두 닫혀 있는 시간대(KST
07:00~09:00, 15:30~17:00 부근)에는 프로그램이 API 호출 없이
대기 상태로 유지된다. 다음 거래 가능 세션이 임박하면(30분 이내)
감시 주기가 짧아지고, 그렇지 않으면 길어진다(`determine_loop_interval_sec`
참고).

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
