# PLAN

## 목적
- `kiwoom_trade`와 같은 운영 감각을 유지하면서 브로커 연동을 한국투자증권 Open API로 전환한다.
- 지금 단계의 핵심은 `실주문 전 검증 구간`을 튼튼하게 만드는 것이다.
- 즉시 필요한 것은 `실시간 시세 확인`, `RSI/SMA 계산`, `paper trading 로그`, `텔레그램 요약`, `간단 실행 구조`다.

## 이번 초기 버전 범위
- KIS OAuth 토큰 발급 구조 작성
- 국내주식 현재가 / 호가 / 일봉 / 분봉 REST 호출 래퍼 작성
- `config/fixed_config.json` + `state/runtime_state.json` 구조 유지
- `python3 run_watch.py`로 바로 감시 가능한 진입점 유지
- 텔레그램 알림 구조 유지
- 실시간 시세 기반 paper trading 기록 유지
- 향후 실주문 확장을 위한 `order-cash` 메서드 자리 마련

## 현재 가정
- KIS 실계좌/모의투자 앱키와 계좌정보는 모두 확보된 상태다.
- 텔레그램은 이미 연결 완료되었고, 필요 시 `~/kiwoom_trade/keys` 값을 재사용할 수 있다.
- 현재는 `모의투자(vps)` 기준으로 인증, 잔고조회, 주문 테스트를 먼저 안정화한 뒤 실계좌 검증으로 넘어간다.

## 다음 구현 순서
1. `KIS_ENV=vps`로 `auth-check` 실검증
2. `balance-check`, `orderable-check`, `order-test --execute`로 모의투자 주문 흐름 검증
3. `indicator-check`로 분봉/일봉 응답 확인
4. `run_watch.py`로 실제 콘솔 감시와 `runtime_state.json` 갱신 확인
5. `paper-run`으로 가상 체결 이력 쌓기
6. 미체결 / 주문조회 / 주문정정취소 API 확장
7. 해외주식 전용 감시/주문 흐름 추가
8. 실시간 웹소켓 체결가/호가 구독 추가
9. shadow mode 검증 뒤 실주문 전환 여부 판단

## 운영 원칙
- `DRY_RUN=false` 이더라도 `LIVE_TRADING_ENABLED=true` 가 아니면 실주문 금지
- 실주문보다 먼저 `데이터 품질`, `알림 품질`, `로그 복구성`을 검증
- 사용자 입장에서 항상 `고정 설정 파일`, `최신 상태 파일`, `실행 진입점`이 명확해야 함
