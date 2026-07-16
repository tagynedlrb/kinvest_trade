# KIS Trade Scaffold

`kiwoom_trade`의 운영 감각을 유지하면서 브로커 연동만 한국투자증권 Open API로 바꾼 단기투자 프로젝트다.  
현재 구조는 `실시간 시세 확인`, `지표 계산`, `paper trading`, `텔레그램 알림`, `모의/실전 계정 분리`, `주문 테스트 CLI`까지 포함한다.
또한 프로세스 간 `KIS 접근토큰 캐시`를 사용해 `1분당 1회` 토큰 발급 제한에 덜 걸리도록 정리했다.
현재 기본 진입점 `python3 main.py` 는 `auto_trade.symbol`에 지정한 해외 종목 1개를 고정 감시하는 `auto-run` 모드로 연결되어 있다.

## 핵심 설계 원칙 (반드시 유지)
이 프로젝트는 세션을 거듭하며 반복적으로 리팩터링되어 왔다. 리팩터링 도중 아래 원칙이 조용히
깨진 적이 실제로 있었으므로(부록의 인시던트 히스토리 참고), 코드를 고칠 때는 항상 아래를 먼저
확인한다. 새 구조를 추가할 때 이 원칙과 충돌하면, 원칙이 아니라 새 구조 쪽을 의심한다.

1. **판단 후 제출이 아니라, 조건 미충족 시 아예 제출하지 않는다.** 진입/청산 조건, 수수료 마진,
   중복주문 방지, 쿨다운 중 어느 하나라도 미충족이면 주문 자체를 만들지 않는다. "일단 시도하고
   하위 계층에서 거부/스킵으로 막는" 방식은 매번 무의미한 왕복과 알림 폭탄으로 이어졌던 재발 패턴이라
   금지한다.
2. **매수 판단은 단일 권위 경로만 갖는다.** `watch_target.action_bias`는 `PriorityStrategyManager.evaluate()`가
   최종 권위이며, 보조 모멘텀 휴리스틱(`evaluate_entry_setup`/`derive_watch_state`)이 독자적으로
   `"BUY"`를 반환해도 전략차단·유동성차단·재진입 쿨다운을 우회해 실매수로 이어져서는 안 된다
   (`lab_watch.py`, 2026-07-14 수정).
3. **`auto-run`과 `liquidity-lab`은 의도된 공존 구조다.** 중복 로직이 아니라 같은
   `momentum_policy.py`를 공유하는 서로 다른 두 실행 모드다. "하나로 합쳐야 하지 않을까"라는
   의문이 들면, 이미 처음부터 그렇게 설계됐다는 뜻이니 통합하지 않는다.
4. **시장별/합산 주문 한도(`max_concurrent_*`)는 리스크 노출 관리용이지 KIS 트래픽 제약이
   아니며, 현재는 전략/시스템 수정 단계이므로 단기 성과를 근거로 임의로 올리거나 내리지 않는다**
   (2026-07-11 원칙 수립, 근거: 과거 이 값들이 성과에 따라 오르내린 것이 실제로는 기술적 근거
   없는 임의 튜닝이었음이 조사로 확인됨).
5. **동적 스캔/풀 갱신은 실패 시 조용히 빈 상태로 수렴하면 안 된다.** 상위 소스(TradingView
   스캔 등)가 실패하거나 가비지를 반환하면, 사람이 개입하기 전까지는 정적 폴백 후보군이나 마지막
   정상 상태로 대체해야 한다 — 그냥 빈 풀로 두고 감시종목이 보유종목만 남는 것은 실패를 감추는
   것이지 안전한 동작이 아니다 (2026-07-14 TV 스캐너 버그 수정 참고).
6. **이 저장소는 public이다.** 새 로그/설정 필드를 추가할 때 계좌번호(CANO), APPKEY/APPSECRET,
   HTS ID를 포함하지 않는다 — 요청 바디나 자격증명 원문을 저장하지 않는다.
7. **신규진입 품질 필터(저가/얇은거래량/넓은스프레드/얇은거래대금)는 이미 보유 중인 종목에는
   적용하지 않는다.** 이 필터들은 "이 종목을 새로 살 만한가"를 판단하는 용도이지 "계속 감시할
   가치가 있는가"를 판단하는 용도가 아니다. 보유 종목이 이 필터에 걸려 스캔 결과(`quote_results`/
   `domestic_ranked`)에서 빠지면, 그 사이클엔 신선한 차트 신호도 계산되지 않아 청산 판단이 오래된
   캐시 신호로 정지된다 — 가격은 계속 움직이는데 매도 판단은 멈추는 상태 (2026-07-16 BCC/FG
   실사건, 부록 참고).

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
  이 `domestic_min_price_krw`(3,000원)는 정적 후보(`domestic_candidates`)와 동적 스캔 결과 모두에 사후 적용되는 범용 하한선이고, `domestic_dynamic_min_price_krw`(5,000원, 더 높음)는 KIS 랭킹 API 요청 자체에 실리는 소스단 사전 필터라 용도가 다르다 — 노이즈가 많은 자동탐색 풀은 소스에서 더 엄격히 걸러내고, 사람이 직접 고른 정적 후보에는 더 완화된 하한선을 적용하는 의도된 이중 기준이다.
- 해외 기본 제외 기준은 `5달러 미만`, `거래량 50만 미만`, `스프레드 0.3% 초과`, `가격×거래량 근사 거래대금 부족`이다.
- 현재 장이 열린 시장에서 `activity_score`가 높은 후보군을 먼저 뽑고, 그 안에서 `signal_score`가 가장 강한 종목을 우선 주문 대상으로 선택한다.
- 해외 후보군은 TradingView Scanner 기반 동적 풀을 우선 사용한다. TV 스캔이 실패/빈 결과를 반환하고 수동 `/lab_relist` 목록도 없으면, `liquidity_lab.overseas_candidates`(기본은 비어 있음)가 설정돼 있는 경우 그 정적 후보군으로 자동 대체하고, 그마저 없을 때만 감시 풀이 완전히 비어 사람에게 `/lab_relist` 지정을 요청한다(2026-07-14, 이 정적 폴백 자체가 죽은 설정으로 방치돼 있던 것을 고쳐서 되살렸다 — 부록 참고).
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
- 미국장이 열려 있으면 진입 조건을 충족한 해외 종목에 동시에 주문한다. 한 사이클에서 최대 `max_concurrent_overseas_orders`(기본 10)개까지 가능하다.
- 국내장이 열려 있으면 진입 조건을 충족한 국내 종목에도 동시에 주문한다. 한 사이클에서 최대 `max_concurrent_domestic_orders`(기본 10)개까지 가능하다.
- 국내+해외를 합친 총 동시 보유 종목 수는 `max_concurrent_total_positions`(기본 `10`)로 제한한다. 기본값 10은 슬롯 예산(`slot_entry_pct=0.10`, 슬롯당 자본의 약 10%) 기준으로 자본 100% 배치에 해당하는 구조적 상한이며, 시장별 한도와 별개로 두 시장 합산 노출을 함께 묶는다. `0`이면 합산 한도를 끄고 시장별 한도만 적용한다. 합산 한도로 해외 신규 진입이 막히면 skip 사유가 `total_position_cap_reached`로 기록된다.
- 시장별/합산 주문 한도는 KIS API 트래픽 제약 때문이 아니라 순수 리스크 노출 관리용 값이다. 현재는 전략·시스템 수정 단계라 단기 성과를 근거로 한도를 올리거나 내리는 조정은 하지 않는 것을 원칙으로 한다. (예외: 2026-07-15, 시장별 한도를 총량 캡과 같은 값(10)으로 맞춘 것은 성과 기반 조정이 아니라, 총량 캡이 이미 실질 자본 노출을 막고 있어 그보다 낮은 시장별 캡이 리스크를 추가로 줄이지 못하고 시장 간 슬롯을 인위적으로 8:8 분배하는 역할만 하던 구조적 중복을 정리한 것 — 부록 참고.)
- 자동 사이클에서는 더 이상 국내 `paper-run` 25초 검증을 끼워 넣지 않는다. 수동 검증이 필요하면 텔레그램 `/lab_paper_test <종목코드>`를 사용한다.
- 해외 고정 손절(`overseas_stop_loss_pct`)은 일시적 wick(단일 체결 급락 후 즉시 회복)에 속지 않도록 확인 단계를 거친다(`overseas_stop_loss_confirm_enabled=true`). 손절 기준을 갓 넘긴 첫 관측이고 매도 거래량 확인이 안 되면 다음 사이클까지 한 번 대기(`stop_loss_confirm_wait`)하고, 다음 사이클에도 손절권이면 그때 손절한다. 단, ①손실이 `overseas_stop_loss_hard_multiplier`(기본 2.0)배를 넘는 깊은 손실이거나 ②현재 분봉 거래량이 평소 대비 `overseas_stop_loss_volume_confirm_ratio`(기본 1.5)배 이상 급증한 음봉(실제 매도세 확인)이면 대기 없이 즉시 손절한다. 손실이 손절권 위로 회복되면 대기 기록은 초기화된다.
- 위 손절 확인과 별개로, 기준가 대비 `overseas_exit_price_shock_pct`(기본 20%) 이상 튀는 극단적 가격은 데이터 오류 가능성이 있어 기존처럼 다음 사이클 재확인 후에만 청산 판단에 사용한다. 두 장치는 계층이 다르다: 손절 확인은 현실적인 1~2% 급락 구간의 회복 가능성 판단, 쇼크 가드는 이상 호가/오염 데이터 차단.
- 기본 매수와 일반 익절/시간청산은 지정가로 제출한다. 다만 `손절`, `ATR 하드스탑`, `모멘텀 손절`, `추세 이탈 손절`, `손실 상태 시간청산` 같은 보호성 청산은 체결력을 우선한다.
- 보호성 청산 주문은 국내는 시장가(`ORD_DVSN=01`, 제출가 0), 해외 실계좌는 시장가, 해외 모의투자는 KIS 안정성을 위해 기준 호가의 공격지정가(`ORD_DVSN=00`)로 제출한다.
- 손익 계산과 텔레그램 표시는 실제 제출가 0이 아니라 청산 판단 당시의 기준 호가(`reference_price`)를 사용한다. 내부 broker audit에는 `order_kind`, `order_division`, `requested_price`, `reference_price`를 함께 기록한다.
- **주문거부 서킷브레이커**: 매도 주문거부는 시장별로 개별 종목 쿨다운(국내 10분/해외 20분)을 걸지만, 매수 주문거부에는 원래 아무 백오프가 없어 같은 오류가 나는 동안 사이클마다 계속 재시도했다. 이제 시장×방향(`domestic:buy`, `overseas:sell` 등) 기준으로 최근 `order_reject_window_minutes`(기본 15분) 안에 `order_reject_threshold`(기본 5)회 이상 주문거부가 쌓이면 그 시장/방향의 신규 주문을 `order_reject_cooldown_minutes`(기본 30분) 동안 중단하고, KIS가 반환한 실제 오류 메시지를 담아 텔레그램으로 즉시 알린다. `/lab_guard`에 `주문거부차단=` 줄로 현재 차단 대상과 누적 건수를 보여주고, `/lab_cb_reset`으로 즉시 해제할 수 있다. `order_reject_threshold=0`이면 기능을 끈다.
- **미체결 청산주문 정체 방지**: 손절/ATR하드스탑 등 보호성 청산은 45초 이상 미체결이면 즉시 취소 후 재주문한다. 반면 `take_profit` 같은 비보호성 청산은 이 조건이 없어, 목표가에 닿지 않으면 해당 주문이 무기한 미체결로 남아 다음 매도 시도를 계속 막았다(예: 2026-07-13 MSEX 익절 주문이 1시간 가까이 정체). 이제 비보호성 청산도 `stale_exit_replace_minutes`(기본 15분)를 넘기면 동일하게 취소 후 현재 호가로 재주문한다. 취소 자체가 실패하면(`pending_exit_cancel_failed`) 위 주문거부 서킷브레이커에도 함께 등록되어, 반복 실패 시 해당 시장/방향이 자동 차단되고 텔레그램 알림이 온다(과거에는 이 경로가 서킷브레이커에 전혀 연결되어 있지 않아 조용히 무한 재시도했다).
과거 CRAN 중복매수 사건, `time_exit_profit` 수수료 미고려 버그, 전수감사로 찾은 로직 버그 9건, TV 스캐너 버그 등 이미 고쳐진 사건의 원인 분석은 [부록: 주요 인시던트 히스토리](#부록-주요-인시던트-히스토리)로 옮겼다. 여기 남긴 항목은 현재도 살아있는 동작 규칙만이다.

현재 기본 후보군과 개잡주 필터 기준은 `config/fixed_config.json`의 `liquidity_lab` 섹션에서 조정할 수 있다.
- 국내: `005930`, `000660`, `035420`, `419050`, `023410`, `010170`, `034940`
- 해외: 기본 고정 후보(`overseas_candidates`)는 비워 두고, TradingView Scanner 결과 또는 `/lab_relist` 수동 목록을 우선 사용한다. 이 필드를 채워두면 TV 스캔이 실패했을 때만 쓰이는 정적 폴백 후보군이 된다.

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

명령이 20개를 넘어가면서 `/lab_help`가 한 줄씩 다 늘어놓으면 알아보기 어려웠다. 이제 `/lab_help`는
아래와 같은 카테고리로 묶어서 보여주고, `/lab_menu`를 보내면 카테고리 버튼(인라인 키보드)이 오는
채팅창 UI로도 탐색할 수 있다 — 버튼을 누르면 그 카테고리의 명령 목록으로 메시지가 바뀌고,
`◀ 메뉴`를 누르면 카테고리 목록으로 돌아간다. 실제 명령 실행은 여전히 텍스트로 입력해야 한다
(버튼은 탐색/조회용이며, 인자가 필요한 명령을 그대로 실행시키지는 않는다).

**🎛 운영 제어**
- `/lab_start`: 즉시 루프 시작
- `/lab_pause`: 현재 사이클은 마무리하고 일시정지
- `/lab_resume`: 일시정지 상태에서 재개
- `/lab_stop`: 현재 사이클 취소 요청 후 정지. 그 시점까지의 누적 거래/손익 요약을 텔레그램으로 전송하고 DB에 기록
- `/lab_terminate`: 현재 lab 실행을 강제 종료하고 대기 상태로 복귀. 그 시점까지의 누적 거래/손익 요약을 텔레그램으로 전송하고 DB에 기록
- `/lab_service_restart`: `kinvest-telegram-control.service` 자체를 재시작

**📊 상태 조회**
- `/lab_status`: 현재 상태, 가상 노출, 최근 반복 매도장애 요약 조회
- `/lab_watchlist`: 현재 감시중인 종목 목록과 `20d/60d`, `5/20` 이평 관계, `vr/mom` 기반 짧은 상태 요약 조회
- `/lab_portfolio`: 실제 계좌 보유, 통합 가상보유, 정산 대기 매도, 누적 성과 조회
- `/lab_guard`: 최근 성과 기준 전략 가드 상태 조회. `차단/감시/참고`와 고정차단(`해외 VWAP단독`, `해외 RSI단독`, `해외 VOL단독`)을 함께 표시한다

**📜 로그 및 성과**
- `/lab_log`: `/lab_start` 이후 세션 기준 실거래/가상거래 손익 요약 조회
- `/lab_performance [시간]`: 최근 N시간(기본 24시간)의 실주문접수 `SELL_REAL`만 전략별로 집계. 감시 신호 `BUY/SELL/HOLD`는 제외
- `/lab_report compare <YYYY-MM-DD|YYYY-MM-DDTHH:MM>`: 기준일/시각 전후 전략별 실주문접수 성과 비교
- `/lab_report wait [시간]`: 최근 N시간(기본 72시간)의 `WAIT` 병목을 시장·전략·사유별로 요약
- `/lab_orders`: 최근 주문 접수/취소/거부 기록, KIS 실시간 미체결 주문, 접수 후 체결확정 추적 필요 주문 조회
- `/lab_gitlog`: 당일 거래/이벤트/주문/텔레그램/API 호출 로그를 CSV 5종으로 정리해 GitHub 저장소에 업로드

**🗄 데이터/성과 초기화**
- `/lab_trim_virtual`: 해외 가상보유가 `max_concurrent_overseas_orders` 한도를 초과했을 때, 손실이 크고 오래된 초과분 정리 후보를 실시간 시세 기준으로 미리보기
- `/lab_trim_virtual_confirm`: `/lab_trim_virtual`이 제시한 초과분 정리 실행(메뉴에는 숨김)
- `/lab_reset`: **가상거래만** 백업 후 초기화(현재 가상보유/노출/한도초과 요약을 먼저 보여준 뒤 확인 요청). `cycle_log`/실거래 이력은 보존된다
- `/lab_reset_confirm`: `/lab_reset`이 제시한 초기화 실행(메뉴에는 숨김)
- `/lab_reset_all`: **전체** 거래이력·성과 초기화(테스트 환경을 처음부터 다시 구성할 때 사용). 아래 별도 설명 참조
- `/lab_reset_all_confirm`: `/lab_reset_all`이 제시한 초기화 실행(메뉴에는 숨김)
- `/lab_cb_reset`: 연속손절 서킷브레이커 및 주문거부 서킷브레이커 강제 해제(**일일손실 한도 정지는 해제하지 않는다** — 그 상태는 KST 날짜가 바뀌거나 `/lab_reset_all_confirm`으로만 풀린다. 2026-07-14 감사에서 README가 이 범위를 과대 서술하고 있던 것을 발견해 정정)

**👀 감시종목 설정**
- `/lab_relist`: 해외 감시 풀을 수동 종목 목록으로 교체(TV 스캔 대신 특정 종목만 보고 싶을 때)
- `/lab_relist_schedule`: 해외 relist 관련 알림 시간 설정

**🧪 테스트**
- `/lab_paper_test <종목코드>`: 지정 국내 종목으로 수동 paper test 실행

**기타**
- `/lab_menu`: 카테고리 버튼(인라인 키보드)으로 명령 목록 탐색
- `/lab_help`: 카테고리별 명령 목록 텍스트로 조회

### `/lab_reset_all` — 전체 초기화 후 실계좌 보유만으로 재구성
`/lab_reset`은 가상거래 3테이블만 지우고 `cycle_log`(매매판단/성과 기록)는 그대로 둔다. 반면
전략 재검증이나 테스트 환경을 완전히 새로 시작하고 싶을 때는 `cycle_log`, `event_log`,
`broker_order_events`, 가상거래 3테이블, `lab_symbol_state`(감시종목 캐시)를 **모두** 지우고
연속손절 카운터·세션 손익·서킷브레이커도 함께 초기화하는 `/lab_reset_all`을 쓴다.

- 실행 전 현재 각 테이블의 건수를 보여주고 `/lab_reset_all_confirm` 입력을 요구한다(오입력 방지).
- 실행 시 DB 파일을 먼저 백업한다(`*_backup_..._pre_reset_all.db`).
- **별도의 "실계좌 잔고 불러오기" 단계가 없다** — `lab_symbol_state`를 비우기만 하면, 다음
  사이클이 실행될 때 매매 루프가 항상 그렇듯 KIS 잔고를 실시간으로 다시 조회해서 캐시를 새로
  채운다. 즉 초기화 직후에는 실제 계좌에 있는 종목만 "보유중"으로 인식되고, 지워진 가상보유는
  더 이상 존재하지 않으므로 과거 성과/이력이 전혀 섞이지 않은 상태로 시작한다.
- `telegram_message_log`/`api_call_log`(운영 감사 로그)는 지우지 않는다 — 이 두 로그는 전략
  성과가 아니라 시스템 동작 이력이라 초기화 대상에서 제외했다.

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
- `/lab_orders`는 내부 주문 이벤트와 KIS 실시간 미체결 주문을 함께 보여준다. 내부 `SUBMITTED` 기록은 체결 확정이 아니므로, `접수 후 체결확정 추적 필요` 섹션에서 `확인필요=MTS/잔고`로 따로 표시한다. live 미체결 조회가 성공하면 `브로커상태=미체결` 또는 `브로커상태=미체결목록없음`도 함께 표시한다.
- **동일한 스킵 이유로 인한 무주문 알림은 30분에 한 번만 보낸다** (2026-07-14, `repeated_skip_notify_cooldown_minutes`). 실제로 주문이 제출되지 않은 채 매 사이클 같은 이유(`net_profit_below_cost` 등)로 계속 대기 상태만 반복되는 경우 텔레그램 알림이 무한히 반복 발송되는 것을 막는다. 실제 매수/매도 체결·주문 오류 알림에는 영향이 없다.
- **장기 미체결 취소는 수동 명령 없이 정책으로만 처리한다** (2026-07-13, `/lab_cancel_stale_domestic`·`/lab_cancel_stale_overseas` 명령 및 확인 명령 삭제). 스케줄러가 매 사이클마다 국내는 정규장 중, 해외는 미국 주문 가능 세션 중에만, 10분에 한 번씩 30분 이상 된 미체결 주문을 조회해 **봇이 직접 제출한 주문만** 자동 취소한다(사용자가 MTS/HTS로 직접 넣은 주문은 건드리지 않음). 예전에는 이 자동 취소가 안 걸릴 때를 대비해 사람이 직접 `/lab_cancel_stale_*`로 강제 취소하는 수단을 남겨뒀는데, 그 배경이었던 해외 미체결 조회 버그(위 CRAN 사건)를 고치고 나니 별도 수동 경로를 유지할 이유가 없어져 삭제했다.

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

## 부록: 주요 인시던트 히스토리
이미 원인을 찾아 고친 사건들의 상세 기록이다. 현재 동작을 이해하는 데는 필요 없고, "왜 지금 이런
방어장치가 있는가"를 나중에 다시 물을 때만 참고한다. 날짜순(오래된 것 먼저)으로 정리했다.

- **해외 미체결 조회 페이지 누락 버그(2026-07-13, CRAN 사건)**: 해외 주문내역 조회(`get_overseas_order_history`)를 모의투자(`vps`) 환경에서 종목 필터 없이(`symbol=""`) + 전체 상태(`fill_filter="00"`, 체결/취소 포함)로 호출하도록 되어 있었다. 이 KIS 모의투자 엔드포인트는 페이지네이션 없이 최대 15건만 반환하는데, 필터를 걸지 않으면 그날 누적된 무관한 이력이 그 15건을 채워버려서 실제로 아직 살아있는 미체결 주문이 조회 결과에서 통째로 사라질 수 있었다. 그 결과 "이미 미체결 매수주문이 있으니 재주문하지 말라"는 중복방지 로직(`_find_open_overseas_order`)이 계속 "없음"으로 오판했고, CRAN 한 종목에 30분 동안 60건 이상의 매수주문이 중복 제출됐다(수량은 매번 남은 매수 가능 금액만큼 줄어들며 자연 감소, 실제 체결/잔고에는 반영되지 않은 상태로 확인됨 — 자금 소진으로 자연히 멈췄을 뿐 코드가 스스로 멈춘 게 아니었음). 종목 필터(`symbol=<대상종목>`)와 미체결 전용 필터(`fill_filter="02"`)를 실제로 넣어보면 모의투자에서도 정상 동작하는 것을 확인했고, 지금은 두 값을 실계좌/모의투자 구분 없이 항상 사용한다. `/lab_orders`·시작/재개 경고·자동 미체결취소 스케줄러가 함께 쓰던 "해외 미체결 전체 조회" 경로도 동일 원인이라 종목별로 나눠 조회하도록 통일했다.
- **자동 미체결취소가 아예 매칭되지 않던 버그(2026-07-13)**: 위 조회 버그를 고치고도 자동 취소가 라이브에서 전혀 동작하지 않아 추가로 찾은 두 번째 원인 — `broker_order_events`에는 주문 접수 응답의 10자리 0-패딩 주문번호(`"0000041501"`)가 저장되는데, 실시간 미체결조회는 패딩 없는 주문번호(`"41501"`)를 돌려줘서 "이 미체결이 봇이 넣은 것인가?" 문자열 비교가 항상 실패했다. 국내/해외 모두 주문번호를 비교하기 전에 앞자리 0을 제거해 정규화하도록 고쳤다.
- **반복 주문거부(`net_profit_below_cost`) 알림 폭탄(2026-07-14)**: 위 CRAN 사건으로 의도치 않게 생긴 5,251주 포지션이 매입가와 현재가가 거의 같아, 시간만료(수익) 청산 조건은 매 사이클 계속 성립하는데 수수료를 반영한 순손익 추정(`_estimate_overseas_net_pnl`)이 0 이하라 매도 자체는 계속 보류(`net_profit_below_cost`)되는 상황이 발생했다. 이 보류는 실제 브로커 주문거부가 아니라 봇이 주문을 내기 전에 스스로 거는 내부 가드인데, `_send_summary`가 이 상태를 매 사이클(여기서는 1분 간격)마다 `동작=주문거부`로 텔레그램에 보내 동일한 메시지가 계속 반복 발송됐다. 1차 조치로 동일한 (시장, 종목, 스킵 이유) 조합의 이런 무주문 알림을 `repeated_skip_notify_cooldown_minutes`(기본 30분) 동안 한 번만 보내도록 억제했다(알림만 억제, 판단 로직은 그대로).
- **근본 원인: "주문을 내고서 막는" 구조였던 `time_exit_profit`(2026-07-14)**: 위 알림 억제는 증상 완화일 뿐, 실제 문제는 `momentum_policy.evaluate_exit_setup`이 시간만료 청산을 판단할 때 수수료를 전혀 고려하지 않고 `pnl_pct >= 0`(가격 기준 총손익)만 보고 매도를 시도하도록 만들었다는 점이었다. 그래서 매입가와 거의 같은 가격에서도 매번 "매도 시도 → 주문 단계(`lab_domestic/overseas_orders.py`)에서 수수료 반영 순손익이 0 이하라 스킵"이라는 무의미한 왕복이 반복됐다. 같은 함수 안의 `marginal_profit_exit`/`partial_profit_lock`/`breakout_exhaustion_exit`/`take_profit`은 이미 수수료를 넘는 최소 마진(`commission_floor = commission_rate*2 + 0.003`, 기본 약 0.8%, 또는 그보다 높은 익절 임계값)을 조건에 포함하고 있었는데 `time_exit_profit`만 이 조건이 빠져 있었다. `pnl_pct >= commission_floor`로 통일해서, 수수료를 못 넘기는 청산은 애초에 매도 시도 자체를 만들지 않고 계속 보유(HOLD) 상태로 남도록 고쳤다 — 조건에 맞지 않으면 주문을 내지 않는 것이 원래 의도였던 다른 익절 사유들과 동일한 방식이다. 이 수정 이후에는 `net_profit_below_cost` 스킵이 `time_exit_profit` 경로에서는 거의 발생하지 않아야 하고(수수료 추정치 차이로 인한 극히 드문 경계값만 안전망으로 남음), 위 알림 억제는 계속 다른 스킵 사유들에 대한 방어선으로 유지된다.
- **전수 감사로 찾은 로직 버그 9건 일괄 수정(2026-07-14)**: git log 전체와 코드베이스를 다시 훑어 아래 문제들을 추가로 고쳤다(전부 회귀 테스트로 재현 후 수정 확인).
  - 서킷브레이커(연속손절/일일한도) 발동 중에는 `_run_cycle`이 `UnboundLocalError`로 매 사이클 예외를 던지고 있었다 — 정작 서킷브레이커가 보호해야 할 그 순간에 보유종목 손절/익절 모니터링이 통째로 멈추던 치명적 버그.
  - 텔레그램 인라인 메뉴 버튼을 더블탭하면(`editMessageText`가 "message is not modified" 오류 반환) 예외가 잡히지 않고 전체 서비스가 크래시됐고, 크래시 시점의 update_offset이 저장 전이라 재시작 후 같은 업데이트가 재전달되어 무한 재크래시 루프에 빠질 수 있었다. 개별 업데이트 처리 예외가 서비스 전체를 죽이지 않도록 `_command_loop`를 강화했다.
  - `force_reentry_after_cycles`(청산 직후 재진입 쿨다운)가 매수신호가 이미 준비된 경우엔 검사조차 되지 않아 사실상 항상 무시되고 있었다.
  - "반복 스킵 알림 30분 쿨다운"이 스킵이 그날의 대표종목(primary_target)이 아닌 다른 시장에 있을 때는 심볼 추적이 끊겨 무력화되던 버그(오늘 만든 기능이 바로 다음 케이스에서 새는 것을 발견) — `_build_action_summary`가 실제 스킵이 있는 시장을 우선하도록 재구성.
  - `/lab_watchlist`가 통화를 가격 크기(1,000 이상이면 원화)로 추측해 표시하고 있어, 저가 미국주식/고가 국내주식에서 통화 표기가 뒤바뀔 수 있었다 — 항목의 실제 `market` 필드를 사용하도록 수정.
  - `/lab_reset_all`이 반복알림 쿨다운/해외신호 실패쿨다운/손절확인 가드 등 일부 캐시를 초기화 목록에서 빠뜨리고 있어 초기화 직후에도 이전 상태가 잔존할 수 있었다.
  - 국내/해외 잔고 조회가 실패하면 "보유종목 없음"으로 처리해 실제 보유종목이 그 사이클 동안 손절/익절 감시에서 완전히 누락될 수 있었다 — 실패 시 직전 캐시로 대체하고 이벤트를 기록하도록 수정.
  - `momentum_loss_cut`(추세 2/3 확인 손절)의 확인 조건 중 하나(`price_below_ma`)가 분봉 장기이평이 아직 준비되지 않은 초기 구간에서 `price < price + 1.0`(항상 참)으로 계산되어, 진짜 확인 조건 1개만 있어도 조기에 손절이 나갈 수 있었다.
  - `partial_profit_lock`/`breakout_exhaustion_exit`도 `time_exit_profit`과 같은 근본원인(수수료 미고려)을 잠재적으로 갖고 있었다 — 현재 배포 설정값(`take_profit_pct=1.5%`/`full_take_profit_pct=2.5%`)에서는 수수료 마진보다 커서 실제로 발동한 적은 없지만, 코드 기본값은 마진 밑이라 설정이 바뀌면 재현될 수 있어 동일하게 `commission_floor` 조건을 추가했다.
  - 해외 매수에는 있던 "이미 미체결 매수주문이 있으면 재주문하지 않고, 오래됐으면 취소 후 재주문" 로직이 국내 매수에는 없었다 — 재시작 타이밍에 따라 국내에서도 중복매수가 재현될 수 있는 경로라 해외와 동일한 로직을 이식했다.
- **국내 매수 주문거부 실사건 조사 + 2차 전수감사(2026-07-14)**: 위 배포 직후 재기동 로그에서 국내매수(`VTTC0012U`)가 며칠째 거의 매번(당일 71건 중 71건) "초당 거래건수 초과"/"MCA 전문바디 구성 오류"로 실패하며 주문거부 서킷브레이커가 30~40분 간격으로 계속 발동 중인 것을 발견했다. `api_call_log`/`broker_order_events` 이력을 직접 대조해 원인을 특정: 국내 매도(`VTTC0011U`)는 정상 동작하는데 매수만 실패하고, 같은 종목이 성공/실패를 오갔으며(코드/바디 문제 아님), 실패 시점마다 서로 다른 종목의 주문이 240ms 안팎 간격으로 연달아 제출되고 있었다(한 사이클에 `max_concurrent_domestic_orders`만큼 여러 종목을 순차 제출 + 오늘 추가한 미체결조회까지 겹쳐 초당 호출 한도를 자체적으로 넘긴 것). KIS REST 클라이언트에 모든 호출을 최소 0.3초 간격으로 강제하는 전역 페이싱(`_throttle`)을 추가해 사후 재시도가 아니라 사전에 폭주를 막도록 수정.
  - 이어서 아직 감사하지 않았던 전략/시그널 관련 파일(`lab_watch.py` 1294줄 — 통합 감시종목/매수매도 대상 선정, `technical_signals.py`, `adaptive_params.py`, `lab_positions.py`, `lab_risk.py`)을 정밀 재검토했다. 두 건이 보고됐고, 하나는 직접 수치 검증 결과 **실제로는 버그가 아니었다** — RSI가 원본(최신순) 배열을 그대로 받는 게 아니라 시간순으로 뒤집어서 계산해야 한다는 주장이었는데, 실제 순수 교과서식 RSI와 대조 계산해보니 현재 코드가 이미 정답과 일치했고 "수정안"대로 바꾸면 오히려 RSI가 반전(뒤집힘)되는 것으로 확인되어 폐기했다(문제가 없는데 무리하게 고치지 않음).
  - 남은 한 건은 **실제 치명적 버그**였다: `build_watch_target_status`가 매수 여부를 두 개의 독립된 판단(정식 `PriorityStrategyManager.evaluate()`의 `strategy_result.signal`과, 별도의 모멘텀 휴리스틱인 `evaluate_entry_setup`/`derive_watch_state`)으로 계산하는데, `strategy_result.signal != "BUY"`이지만 `derive_watch_state`가 독립적으로 "BUY"를 반환하는 경우, 전략차단/유동성차단/VWAP·RSI 확인대기/**재진입 쿨다운**을 전혀 거치지 않고 그대로 `action_bias="BUY"`가 나갔다. `select_domestic_buy_targets`는 `action_bias=="BUY"`만 보고 그대로 매수 후보로 채택하고(국내는 어떤 재검증도 없음), `select_overseas_buy_targets`는 전략차단만 부분적으로 재검증할 뿐 유동성차단/쿨다운은 재검증하지 않아, 해외도 마찬가지로 새어나갈 수 있었다. 즉 손절 직후 재진입 쿨다운 중인 종목이 이 경로로 즉시 재매수될 수 있는 실제 우회로였다(같은 파일의 "stale signal cache" 분기는 정확히 이 이중신호 위험을 이미 인지하고 항상 WAIT로 억제하고 있었는데, 정작 평시(live) 경로에는 동일한 처리가 빠져 있었다). `derive_watch_state`만으로 나온 BUY는 실거래 경로(live path)에서도 동일하게 항상 WAIT로 억제하도록 수정.
- **초기 설계 목표 대비 구조 표류(drift) 전수 조사(2026-07-14)**: "몇십 번의 자동 개선을 거치며 구조가 꼬인 것 같다"는 우려에 따라, `WORKLOG.md` 전체(4,276줄)와 `git log --oneline --all`(253개 커밋) 기준으로 초기 설계 목표부터 지금까지의 설계 의도 변천사를 다시 정리했다. 결론: 이 프로젝트는 이미 여러 차례(2026-07-11 지시문 #68 전체 점검, 2026-07-14 1·2차 전수감사) 스스로 표류를 탐지하고 수정해 온 상태였고, `auto-run`/`liquidity-lab` 공존, `momentum_policy` 공유, 시장별/합산 한도 임의조정 금지 원칙 등 핵심 구조는 모두 처음 의도대로 유지되고 있었다. 유일하게 확인된 실제 표류는 위 "치명적 버그"(2026-07-02 `a75e3f5` 커밋이 세운 "watch_target에 도달하면 이미 게이트를 통과했다"는 전제가, 이후 `lab_watch.py` 분리/확장 과정에서 조용히 깨진 것)였고 이미 수정 완료 상태였다. 재발을 막기 위해 이런 "이 경로는 이미 검증됐다"는 전제들을 코드 주석이 아니라 회귀 테스트/원칙 문서로 명문화하기로 하고, 위 [핵심 설계 원칙](#핵심-설계-원칙-반드시-유지) 섹션을 이 README 맨 앞에 추가했다.
- **TradingView 스캐너가 거래소 정보 없는 컬럼을 잘못 신뢰한 버그(2026-07-14)**: 위 표류 조사와 별도로 "감시종목이 보유종목 2개뿐이고 새 후보가 안 보인다"는 지적을 실사건으로 조사했다. `tv_scanner.py`의 `_parse_tv_symbol`이 TradingView 응답의 `d`/`name` 컬럼(거래소 표기가 전혀 없는 맨 티커, 예: `"SNEJF"`)을 파싱하면서 콜론(`:`)이 없으면 무조건 `NASD`로 간주하고 있었다. 실제 거래소 정보는 각 행의 최상위 `s` 필드(예: `"OTC:SNEJF"`, `"NYSE:APO/PA"`)에만 들어 있는데 이 필드를 아예 읽지 않고 있었던 것이다. 그 결과 OTC 장외 페니주(5글자+`Y`로 끝나는 전형적 ADR 티커: `SNEJF`, `PCRHY`, `DNKEY`, `TRMLF`, `MAUTF`, `TTDKY`, `CSCCF` 등)와 우선주(`APO/PA`, `HPE/PC`, `ARES/PB`)가 매 스캔 30종목 풀 상당수를 채우고 있었다. 이 종목들은 KIS 시세 조회에서 빈 응답이거나 실거래량이 0에 가까워 하위 유동성 필터에서 거의 전부 제외됐고, 그 결과 최종 감시목록이 보유종목만 남는 상태가 반복됐다. 실제 KIS API로 라이브 대조(수정 전/후 풀 내용)까지 마쳐 원인을 확정했다. `s` 필드 기반 파싱 + 우선주/워런트 제외로 수정.
- **설정/텔레그램 3차 전수감사(2026-07-14)**: 위 두 조사와 병행해 설정(`config.py`/`fixed_config.json`)과 텔레그램 명령어 체계를 별도로 감사했다. 발견 및 수정:
  - `liquidity_lab.overseas_candidates`가 완전히 죽은 설정이었다 — TV 스캐너가 주 소스가 된 이후 어디서도 읽지 않아, 이 값을 JSON에 채워도 아무 효과가 없었다. TV 스캔이 실패했을 때 기대되는 정적 폴백 역할을 못 하고 있었던 것 — 위 TV 스캐너 버그와 맞물려 있던 구조적 취약점이라, TV/수동목록 모두 없을 때의 최종 폴백으로 다시 연결했다.
  - 일일손실 서킷브레이커의 `operating_capital_krw` 폴백(`getattr(..., 0) or <값>`)이 실제 기본값(5,000만원)과 다른 500만원으로 박혀 있었다 — 지금은 값이 항상 정상 설정돼 있어 실제로 발동한 적 없는 잠재 위험이었지만, 값이 0이 되는 순간 임계값이 10배 좁아지는 함정이라 기본값과 일치시켰다.
  - `overseas_exit_mid_mismatch_pct`/`overseas_exit_price_shock_pct`/`overseas_exit_price_shock_confirm_pct` 3개 청산 가드 값이 코드에만 하드코딩돼 있어 `fixed_config.json`으로 조정할 수 없었다 — 데이터클래스와 설정 파일에 정식으로 추가해 다른 `overseas_*` 값들처럼 조정 가능하게 만들었다.
  - `AutoTradeConfig.inverse_etf_symbols`/`leveraged_etf_symbols`는 `auto_trade` 섹션에 있는 필드인데도 실제로는 `liquidity_lab` JSON 섹션 값을 그대로 읽는다 — 두 실행 모드가 인버스/레버리지 ETF 판정을 반드시 동일하게 해야 해서 의도된 공유지만, `auto_trade`에 이 키를 넣어도 무시된다는 사실이 코드에 문서화돼 있지 않아 주석으로 명시했다.
  - `/lab_cb_reset`의 README 설명이 실제 동작보다 범위를 과대 서술하고 있었다(위 Telegram Control 섹션에서 정정).
  - 그 외 명령어 핸들러/메뉴/도움말 정합성, 판단-제출-차단 재발 패턴, 국내 동적 스캔(KIS 랭킹 API 기반, TV류 버그 없음)은 감사 결과 모두 정상으로 확인되어 수정하지 않았다.
- **국내매수 100% 실패 재발 — 페이싱이 인스턴스별로 갇혀 있던 진짜 근본원인(2026-07-15)**: 전날 배포한 "모든 호출 최소 0.3초 간격" 페이싱이 사실상 듣지 않아, 재기동 후 8시간 동안 거의 모든 엔드포인트가 30~42% 실패했고 국내매수는 32건 중 32건(100%) 전부 실패했다. 원인은 `_rate_limit_lock`/`_last_request_at`이 `self.`(인스턴스) 속성이었던 것 — `_run_cycle`이 매 사이클 새 `KisRestClient()`를 만들고, `/lab_portfolio`·`/lab_status`·gitlog 업로드 등도 각자 별도의 임시 클라이언트를 열어 메인 루프와 동시에 호출하는데, 인스턴스별 페이싱은 그 객체를 통한 호출끼리만 간격을 보장해서 서로 다른 인스턴스들이 계정 전체의 초당 한도를 나눠 쓰지 못하고 경합했다. `client.py`의 페이싱 상태를 클래스 속성으로 바꿔 프로세스 내 모든 인스턴스가 하나의 시계를 공유하도록 수정했다(통제된 테스트로 여러 인스턴스 동시 호출에서도 정확히 최소 간격이 지켜짐을 직접 검증). 배포 후 라이브 관찰 결과 개별 시세조회 호출 레벨의 잔여 실패(~10~20%대, KIS 모의투자 서버 자체의 공유 인프라 특성일 가능성이 높고 기존 재시도 로직이 흡수)는 일부 남았지만, 서킷브레이커 발동·주문 실패 등 실제 영향 지표는 배포 후 완전히 0건으로 확인했다. 간격값은 0.3초 → 0.5초 → 0.7초로 라이브 검증을 거치며 상향했다.
- **국내매수 100% 실패, 2차 근본원인 — 소수점 가격 문자열이 매번 거부됨(2026-07-15)**: 위 페이싱 수정 배포 후에도 국내매수(`VTTC0012U`)가 4.5시간 동안 9건 중 9건(100%) 계속 `IGW00007`로 실패했다(이번엔 시도 간격이 5~8분이라 레이트리밋과 무관함을 확인). `lab_domestic_orders.py`의 매수 경로가 `buy_price = float(...)` 이후 정수 변환 없이 그대로 `place_cash_order`에 넘기고 있어, `client.py`가 `"ORD_UNPR": str(price)`로 문자열화할 때 `"80000.0"`처럼 소수점이 포함된 문자열이 제출되고 있었다 — 원화는 소수 단위가 없는데도. 매도 경로는 이미 `int(...)` 캐스팅을 거치고 있어 매도만 정상 동작해 온 것과 정확히 대응된다. `submit_price = int(buy_price)`로 수정. **2026-07-14에 이 정확한 float-vs-int 가설이 한 번 제기됐다가 "이 float 코드가 과거 정상 동작 기간 이전부터 있었다"는 git blame 근거로 기각된 적이 있는데, 그 추론은 버그를 배제하는 증거가 되지 못했다** — 코드가 오래됐다는 것과 그 경로가 그 시점에 실제로 실행됐다는 것은 별개다. 같은 조사에서 해외 거래는 실제로는 활발했음을 확인했다 — TV 스캐너 수정 이후 정상화된 풀에서 새벽에 신규 가상포지션 6개가 40분 만에 열려 `max_concurrent_overseas_orders=8` 한도에 도달해 대기 중이었을 뿐, 버그가 아니라 설계된 동시보유 한도였다.
- **국내매수 100% 실패, 3차 재조사 — 소수점 가격 수정으로도 해결되지 않음, 근본원인 재오픈(2026-07-15)**: 위 수정을 "해결"로 기록했지만, 다음날 실사간 재확인 결과 배포 후에도 오늘 하루 국내매수 36건 중 36건(100%)이 여전히 동일한 `IGW00007`로 실패했다 — 성급한 결론이었음을 인정한다. 실패 시점의 실제 종목/수량/가격을 그대로 `client.place_cash_order`로 재현 호출했더니(장마감 후, 모의계좌라 안전) 매번 `모의투자 장시작전입니다`만 깨끗하게 반환됨 — 즉 같은 바디가 스키마 파싱은 통과해 시장시간 검사까지 도달하므로 "바디가 깨져 있다"는 설명과 맞지 않는다. 로컬에 클론된 KIS 공식 샘플 저장소(`/tmp/open-trading-api`, `examples_llm/domestic_stock/order_cash/order_cash.py`, 동일 tr_id 사용)와 `place_cash_order`의 바디를 필드 단위로 대조한 결과 완전히 일치, 실패 가격들의 KRX 호가단위(ETF/ETN 전용 5원 틱 기준 재계산)도 전부 유효, 가용 예수금도 충분 — 바디 형식·틱단위·가용자금 가설을 모두 배제했다. 남은 가설은 모의투자(VTS) 매칭엔진 단계의 계정/세션별 문제로 좁혀지나, 장 마감 후에는 시장시간 검사에서 조기 반환되어 그 다음 단계를 재현할 수 없어 오늘은 확정하지 못했다. **소수점 가격 수정 자체는 KRW 소수단위가 없다는 점에서 여전히 올바르고 유지해야 하지만, 국내매수 100% 실패의 전체 원인은 아니었다** — 다음 국내 정규장(09:00 KST)에 실시간으로 바디 변형을 하나씩 시도해 원인을 좁혀야 하는 미해결 상태.
- **시장별 동시보유 한도 완화(2026-07-15)**: `max_concurrent_overseas_orders`/`max_concurrent_domestic_orders`가 `8`인 이유를 git 히스토리로 추적한 결과, 2026-07-10 해외 손실 집중 분석 후 `20 -> 8`로 낮춘 근거 있는 조정이었으나, 바로 다음날 `max_concurrent_total_positions=10`(자본 기준 총량 캡)이 신설되고 손실 원인이었던 단독 신호 진입 자체를 막는 진입-품질 가드도 함께 추가되어, 시장별 캡 `8`은 이미 상위 총량 캡보다 낮아 리스크를 추가로 줄이지 못하고 시장 간 슬롯을 8:8로 인위 분배하는 역할만 하고 있었다. 총량 캡(10)과 일치시켜 `8 -> 10`으로 상향 — 실질 자본 노출은 총량 캡이 그대로 막아준다.
- **"overseas_position_cap_reached"가 주문거부로 오분류되던 문제(2026-07-16)**: 해외 동시보유 한도에 도달해 신규 진입을 정상적으로 건너뛴 상태(설계된 동작, 버그 아님)가 `_IGNORED_SKIP_REASONS`에 없어 "의미 있는 스킵"으로 집계되며, 30분 쿨다운마다 `[KIS][거래알림] 동작=주문거부 사유=overseas_position_cap_reached`로 반복 발송되고 있었다. 실제로는 아무 문제가 없는데 매번 "거부"로 알리는 것은 오히려 잘못된 경보다. `overseas_position_cap_reached`/`total_position_cap_reached`를 무시 목록에 추가해 이 경우 알림을 보내지 않도록 수정.
- **`/lab_portfolio` 응답 지연(2026-07-16)**: 두 가지 중복 지연 요소를 발견. (1) `load_live_virtual_price_lookup`이 이미 프로세스 전체에 적용 중인 KIS 클라이언트 페이싱(0.7초/호출) 위에 배치당 추가로 `asyncio.sleep(1.05)`를 걸고 있어 이중 페이싱이었다 — 제거. (2) `build_portfolio_message`는 이미 `last_report.watch_targets`/`lab_symbol_state`에서 최근(대개 20초 이내) 가격을 가져와 표시하는데, `send_portfolio_message`는 이 캐시가 신선한 상태(루프 실행 중 + 상태 지연 임계치 이내)에서도 매번 보유 가상종목 전부에 대해 실시간 시세를 다시 조회하고 있었다 — 정확도 개선 없이 종목 수에 비례한 지연만 추가하던 것. 캐시가 신선하면 이 재조회를 건너뛰도록 수정(루프가 멈춰 있거나 데이터가 오래됐을 때는 그대로 실시간 재조회).
- **BCC/FG가 3%+ 수익에도 매도 안 되고 watchlist에 "캐시" 신호로 계속 뜨던 실사건(2026-07-16)**: `scan_overseas()`/`scan_domestic()`의 신규진입 품질 필터(저가/얇은거래량/넓은스프레드/얇은거래대금)가 held 여부와 무관하게 모든 후보에 적용되고 있었다. BCC/FG는 실시간 시세의 거래량/스프레드가 순간적으로 필터에 걸려 `quote_results`에서 제외됐고, 그 결과 해당 사이클엔 차트 신호가 계산되지 않아 `_signal_cache`에 값이 안 남았다. 이후 watch target 생성 시 `signal_snapshot is None`이라 마지막으로 캐시된(오래된) 신호로 청산 판단을 하는 폴백 경로를 타면서, 화면엔 "신호=캐시"로 표시되고 정작 최신 가격 움직임을 반영한 청산 판단은 멈춰 있었다. 신규진입 필터는 원래 "새로 살 만한가"만 판단해야 하는데 "계속 감시할 가치가 있는가"까지 판단해버린 것 — held 종목은 이 필터에서 예외 처리하도록 수정(해외/국내 모두). 위 [핵심 설계 원칙 7번](#핵심-설계-원칙-반드시-유지)으로 명문화.
- **watchlist "신호=캐시" 표현 개선 + 보유수량 항상 표시(2026-07-16)**: 위 근본원인 수정과 별도로, "신호=캐시"라는 표현 자체가 무슨 뜻인지 알기 어렵다는 지적에 따라 "신호=갱신지연(직전값 사용)"으로 더 직관적으로 바꿨다. 또한 보유 중인데 실시간 손익 조회가 아직 안 된 경우 `보유=N주` 표시 자체가 통째로 사라지던 것을 고쳐, 손익만 `손익=조회중`으로 표시하고 보유수량은 항상 보이게 했다.
- **국내종목 텔레그램 표기를 종목코드 우선에서 한글 종목명 우선으로 변경(2026-07-16)**: 기존엔 `005930(삼성전자)`처럼 코드가 먼저 나왔는데, `삼성전자(005930)`로 이름을 앞에 오도록 바꿨다(`format_domestic_symbol_label`, `message_format.py`). 이름이 12자를 넘으면 말줄임표로 잘라 한 줄 메시지가 너무 길어지지 않게 했다. 추가로, 보유 종목의 이름이 그날의 거래량/등락율 순위 풀에 없으면 이름을 아예 못 찾는 문제도 발견해 수정 — 잔고조회(`get_balance`) 응답에 이미 담겨 있는 `prdt_name` 필드를 이름 맵에 직접 채우도록 해, 순위권 밖의 보유종목도 항상 한글명이 표시된다.
- **거래내역 전수분석 — 해외 실거래 11% 승률의 실체는 진입 직후 노이즈성 손절(2026-07-16)**: "타겟 선정과 매도에 문제가 많은 것 같다"는 지적에 `scripts/analyze_trades.py --days 7`로 전수 분석. 해외 실거래 19건 중 74%(14건)가 `trend_filter_lost` 청산이었고, 이 14건의 보유시간이 거의 전부 5~45분(다수가 5~6분에 몰림)에 가격 변동폭은 -0.005%~-0.43%(잡음 수준)였다 — 반면 유일한 두 수익 거래는 19분/1100분(≈18시간)을 버틴 경우였다. 진입 시점 RSI(35~58, 중립)와 breakout거리(≈0)를 보면 "과열 추격매수" 패턴은 없어 타겟 선정 자체보다, `min_hold_before_trend_exit`(12사이클=5분)이 관찰된 최소 보유시간과 정확히 일치한다는 점에서 **막 진입한 자리(이동평균 교차 직후)에서 노이즈 한 번에 청산 조건이 성립해버리는 것**이 핵심 원인으로 확인됐다. 이 정확히 같은 진단이 2026-07-10에도 있었고(`5 -> 12`로 이미 한 번 완화) 6일 뒤 같은 비율로 재발한 것은 그 완화폭이 부족했다는 뜻 — `12 -> 30`(약 12.5분, 5분봉 2.5개 분량)으로 추가 완화했다. `atr_hard_stop`/`take_profit`/`time_exit_profit`은 `hold_cycles` 게이트가 없어 이 변경과 무관하게 즉시 작동하므로, 진짜 급락 방어는 그대로이고 "잡음에 잘리는" 케이스만 완화된다. 전략 파라미터 튜닝이라 코드 테스트로는 검증 한계가 있어, 다음 거래일 실거래 결과로 재확인 필요.
- **텔레그램 컨트롤 서비스가 네트워크 순단으로 크래시(2026-07-16)**: `_command_loop`의 텔레그램 롱폴링 `get_updates()` 호출 도중 발생한 일시적 `httpx.ReadError`(네트워크 단절)가 그대로 전파되어 `run()`의 `asyncio.gather`를 깨뜨리고 전체 프로세스(스케줄러 루프 포함)가 종료됐다. 개별 업데이트 처리(`_handle_update`)는 이미 try/except로 보호돼 있었지만 `get_updates()` 호출 자체는 그 가드 밖에 있었던 것. `get_updates()`를 try/except로 감싸 네트워크 예외 시 3초 대기 후 재시도하도록 수정.
- **손절 반복 종목이 쿨다운만 지나면 같은 패턴으로 재손실을 반복(2026-07-16)**: 거래내역을 종목 단위로 재집계하니 BSBR/CCIX/FHN/RQI/WNC가 `trend_filter_lost` 손절 후 재진입 쿨다운이 끝나자마자 같은 전략 신호로 재매수되어 다시 같은 사유로 손실을 낸 사례가 다수 확인됐다(재진입 자체를 막는 장치는 있었지만, "같은 종목의 연속 손실"을 억제하는 장치는 없었음). `register_exit_cooldown`에 종목별 연속손실 스트릭을 추가해, 손실이 연속 2회면 쿨다운을 최소 60분, 3회 이상이면 최소 180분으로 상향(수익 청산 시 스트릭 리셋)해 스캐너가 다른 후보를 찾을 시간을 벌도록 했다. 강제손절/익절 판단(보유 중 청산 로직)에는 영향 없음 — 재진입(신규 매수) 쿨다운에만 적용.

## 참고한 공식 자료
- KIS API 포털: https://apiportal.koreainvestment.com/
- KIS 공식 샘플 저장소: https://github.com/koreainvestment/open-trading-api
