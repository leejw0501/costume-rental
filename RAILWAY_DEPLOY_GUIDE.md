# 코스튬 대여몰 V3.3.1 - Railway 테스트 서버 배포 가이드

## 1. 이 버전의 저장 구조
- 로컬 실행: 기존처럼 프로젝트 폴더의 `rental.db`, `static/uploads/` 사용
- Railway + Volume: Railway가 제공하는 `RAILWAY_VOLUME_MOUNT_PATH`를 자동 감지
  - DB: `<volume>/rental.db`
  - 상품 이미지: `<volume>/uploads/`
  - 관리자 입력 API 비밀파일: `<volume>/.payment_secrets.json`, `<volume>/.notification_secrets.json`
  - 세션 키 파일(환경변수 미사용 시): `<volume>/.secret_key`
- Volume의 DB가 처음 비어 있으면 ZIP에 포함된 테스트 `rental.db`를 자동 복사합니다.
- Volume의 uploads가 비어 있으면 포함된 테스트 이미지를 자동 복사합니다.

## 2. GitHub에 올리기
1. 이 폴더의 파일 전체를 새 GitHub 저장소에 업로드합니다.
2. `.env`, `.secret_key`, `.payment_secrets.json`, `.notification_secrets.json`은 커밋하지 않습니다.
3. 테스트용 `rental.db`와 `static/uploads/`는 최초 Railway 테스트 데이터를 만들기 위해 V3.3.1에는 포함되어 있습니다.

## 3. Railway 프로젝트 만들기
1. Railway 로그인
2. New Project → Deploy from GitHub Repo
3. 위 GitHub 저장소 선택
4. 배포가 시작됩니다. `railway.json`의 Gunicorn 시작 명령과 `/health` 체크를 사용합니다.

## 4. Volume 연결 - 반드시 권장
1. Railway 프로젝트 Canvas에서 Volume 추가
2. Flask 서비스에 연결
3. Mount Path를 `/data`로 지정
4. Railway는 `RAILWAY_VOLUME_MOUNT_PATH=/data`를 런타임에 자동 제공합니다.
5. 재배포 후에도 주문 DB와 업로드 이미지가 `/data`에 남습니다.

중요: Volume 없이 테스트하면 배포/재시작 시 SQLite DB와 업로드 파일이 영구 보존된다고 가정하면 안 됩니다.

## 5. Variables 설정
Railway 서비스 → Variables에서 최소 다음 값을 설정하는 것을 권장합니다.

### 필수 권장
`APP_SECRET_KEY`
- 충분히 긴 임의 문자열
- 예: 로컬에서 `py -c "import secrets; print(secrets.token_hex(32))"`로 생성

### 토스 테스트 결제를 할 경우
`TOSS_CLIENT_KEY`
`TOSS_SECRET_KEY`

### SOLAPI 실제 테스트를 할 경우
`SOLAPI_API_KEY`
`SOLAPI_API_SECRET`
`SOLAPI_WEBHOOK_SECRET`

환경변수로 값을 넣으면 관리자 입력 파일보다 환경변수가 우선됩니다.

## 6. 공개 도메인 생성
Railway 서비스 → Settings/Networking → Generate Domain

생성 예:
`https://xxxx.up.railway.app`

고객 화면:
`https://xxxx.up.railway.app/`

관리자 로그인:
`https://xxxx.up.railway.app/admin/login`

서버 상태확인:
`https://xxxx.up.railway.app/health`

## 7. 최초 외부 테스트 전에
- 관리자 기본 비밀번호를 반드시 변경합니다.
- 관리자 → 운영설정에서 보증금 입금 계좌를 확인합니다.
- 결제연동은 처음에는 시뮬레이션 모드로 확인합니다.
- SOLAPI 자동발송은 처음에는 OFF 상태를 권장합니다.
- TEST 주문/전화번호로 실제 메시지가 나가지 않는지 확인합니다.

## 8. 권장 통합 테스트 순서
1. 휴대폰 LTE/5G에서 외부 URL 접속
2. 회원가입/로그인
3. 상품 상세 → 대여일/기간/사이즈 선택
4. 장바구니 → 주문
5. 카드 결제(처음에는 시뮬레이션, 이후 Toss Test)
6. 관리자 로그인 → 주문 확인
7. 보증금 `입금대기 → 입금확인`
8. 고객 마이페이지에서 보증금 상태 확인
9. 발송/회수/반납 처리
10. 취소/부분환불/보증금 환급 흐름 확인

## 9. 주의: SQLite 운영 범위
V3.3.1은 테스트 서버 단계이므로 SQLite + Gunicorn 1 worker를 사용합니다.
실제 고객이 다수 동시 접속하는 정식 운영 단계에서는 PostgreSQL 전환을 권장합니다.
