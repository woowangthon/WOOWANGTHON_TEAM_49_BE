# 월급잇다 통합 백엔드

FastAPI 백엔드와 agent를 **Python 3.14 한 환경**에서 실행한다. 별도 agent HTTP 서버는 없다. Next.js는 별도 프로젝트에서 이 API를 사용한다. 이번 결과물에는 운영 프론트 화면을 포함하지 않는다.

## 설치와 실행

터미널의 현재 디렉터리를 이 저장소의 루트(`app`, `agent`, `pyproject.toml`이 있는 폴더)로 설정한다.

```powershell
uv sync --locked
Copy-Item .env.example .env
uv run python -m app
```

API: `http://127.0.0.1:8000`, Swagger: `http://127.0.0.1:8000/docs`, 상태: `/health`.

macOS/Linux bash에서는 환경파일 복사만 다음과 같이 바꾼다.

```bash
uv sync --locked
cp .env.example .env
uv run python -m app
```

- 디지털 PDF 읽기, 계산·대조, 템플릿 설명·질문·문의, PDF·ZIP은 모델 키 없이 실행한다.
- AI 추출 A, 선택적 PDF 보완 B, 자유 채팅에는 `.env`의 `OPENAI_API_KEY`가 필요하다. 키가 없으면 명시적 오류를 반환한다. 가상 고정 응답으로 대체하지 않는다.
- B는 사용자 선택 항목을 대상으로 **전체 PDF를 외부 모델에 전송**하는 별도 작업이다. 업로드·읽기·계산은 B를 자동 실행하지 않는다.
- 스캔 PDF/JPEG/PNG 읽기는 Tesseract 실행파일과 `kor`, `eng` 언어 데이터가 필요하다. `AGENT_OCR_COMMAND`, `AGENT_OCR_DATA_PATH`를 배포 환경에 맞게 지정한다. 이미지 원본과 파생 PDF를 별도로 보관한다.
- 한글 보고서 폰트는 `assets/fonts`에 라이선스와 함께 포함했다. `AGENT_FONT_PATH`로 다른 배포 폰트를 지정할 수 있다.
- 앱 데이터는 `DATA_DIR=./data` 아래에 새로 생성한다. 기존 원본 DB/환경파일을 사용하거나 복사하지 않는다.
- 단일 프로세스로 실행한다. 기본 `WORKER_CONCURRENCY=1`, 최대 4이며 사례/문서 범위의 중복 실행도 제한한다. `uvicorn --workers N` 다중 프로세스 배포는 지원하지 않는다.

## 처리 흐름

1. 회원가입·로그인 → 사업장과 급여기간 사례 생성 → 자료함 업로드/사례 연결.
2. 원문 읽기 작업 → 저장 읽기 확인 → 명시적 추출 A → 필요한 항목만 보완 B → 사용자 후보 선택/수정/보류.
3. 사례의 기본 또는 추가 비교 묶음에 문서 선택 → 확인 입력 저장 → analyze 요청.
4. 백엔드가 입력 스냅샷을 고정하고 `await run_task(...)` 실행 → 실행권·취소·소유권·삭제·버전 재검사 → 결과 저장.
5. 확정 결과를 선택해 설명·문의·보고서 생성 → 인증된 다운로드. 과거 결과 보고서는 당시 입력을 사용한다.

모든 임금 계산과 대조는 agent 규칙을 사용한다. 지원 범위는 표준 시급/월급 조건이며, 정보 부족·모름·보류·충돌을 0으로 계산하지 않는다. `COMPLETED`는 작업 저장 완료이지 모든 수당 계산 완료라는 뜻이 아니다.

## 1차 문서 검토 작업실 API

사업장·사례 생성 전에도 로그인한 사용자 소유의 검토 초안을 만든다. 여러 원본, 페이지별 판독·좌표, 구간별 후보, 사용자 수정·확정 이력과 작업 상태를 SQLite에 저장한다. 기존 업로드 API는 판독 후 A 추출까지 자동 실행한다(API 키 필요). B는 별도 명시 요청이다.

1. `POST /api/v1/reviews`로 초안 생성. `auto_process=false`, `auto_verify=false`가 기본이다.
2. `POST /api/v1/reviews/{id}/documents`에 multipart `revision`과 반복 `files`를 전송한다. 한 번에 최대 20개, 파일당 설정 한도(기본 20MB), 요청 합계 80MB다. 같은 검토의 동일 바이트 재등록은 자료·모델 호출을 중복 생성하지 않는다.
3. `auto_process=true`를 명시하면 등록 후 판독→분류→구간별 추출을 실행한다. 기본 모드에서는 로컬 판독 후 사용자가 `process`를 요청한다. 혼합 PDF는 겹치지 않는 페이지 구간으로 구분하고, 불명확한 유형·경계는 수동 지정한다.
4. 원문 페이지 이미지와 좌표, 후보, 지급·공제·근태·입금의 행 묶음을 조회해 값을 수정·보류·확정한다. 위치가 불확실한 값은 `location_status=unavailable`로 반환한다. 재처리로 수정·삭제한 값을 덮어쓰거나 사용자 확정을 자동 복원하지 않는다.
5. 기본 정보·동일 대상 여부·계산에 사용할 값을 확인한 뒤 `POST .../complete`로 기존 사례·비교 묶음에 연결한다. 확정한 값만 불변 입력으로 변환하고 기존 `run_task()`로 계산·대조한다. 기존 설명·문의·PDF·원본 ZIP API를 그대로 사용한다.

자동 처리에는 API 키가 필요하며 가짜 성공 응답은 없다. 새 검토의 분류·추출은 저장 판독문을 전송한다. `auto_verify=true` 또는 명시적 `verify` 요청은 대상 원문 페이지 이미지를 외부 모델에 전송한다(구간별 최대 50항목·6페이지, 한 차례). 기존 B는 전체 PDF 전송을 유지하며 새 검토의 페이지 이미지 재검증과 구분한다. 정상 후보·모델 간 일치는 사용자 확정을 대신하지 않는다.

검토 요청은 `revision`으로 동시 수정을 검사한다. 취소·소유권·원본 해시·작업 실행권을 처리 중과 게시 시 재검사한다. 서버 중단 작업은 `INTERRUPTED`가 되며 사용자 `resume` 요청으로만 재개한다. 완료된 모델 응답과 누적 호출 예산·사용량은 DB에 보존한다. 기존 작업과 새 검토 작업은 같은 `WORKER_CONCURRENCY` 제한을 공유한다.

입금 문서의 추출·확정은 원금 배분을 생성하지 않는다. 연결된 입금 자료의 후보·근거를 기존 거래 등록·사례 배분 API로 등록한 뒤 비교 묶음에서 거래 ID를 선택한다. 표준 임금 조건과 근무기록은 확인된 `calculation_input` 또는 기존 비교 묶음 입력 API로 명시한다. 확인하지 않은 시간·조건을 임의로 계산하지 않는다.

Next.js 화면, AI 추천·임시 적용, 검토 전용 안내 챗봇과 실행 그래프 UI는 이번 1차 범위에 포함하지 않는다. 기록 조회 API는 제공한다. 초안 삭제와 연결 후 사례·원본 삭제는 별도 동작이며, 전체 사본·백업 영구삭제 운영은 기존 잔여 과제로 유지한다.

## 검증

```powershell
uv run pytest tests/integration -q
uv run pytest agent/tests -q
uv run ruff check app agent tests/integration
```

Windows의 제한된 실행 샌드박스는 asyncio 내부 localhost 소켓을 차단할 수 있다. 이 경우 테스트가 이벤트 루프 생성에서 멈추므로 localhost 소켓을 허용하는 환경에서 실행한다.

실제 검증 결과·대역 범위·잔여 작업은 [검증 기록](docs/VERIFICATION.md), [요구사항 검토표](docs/REQUIREMENTS_MATRIX.md)를 확인한다. 실제 모델/OCR 품질, 실제 Next.js 화면, 광범위한 법률 적합성은 통합 테스트 통과와 별개다.

## 문서

- [첨부 프론트 API 명세 대조·전체 테스트 결과](docs/FRONTEND_CONTRACT_AUDIT.md)
- [EC2 HTTP 환경파일·Docker 빌드·기동](docs/DEPLOY_EC2.md)
- [폴더·호출·저장 구조](docs/ARCHITECTURE.md)
- [새 검토 API와 연결 계약](docs/REVIEW_WORKSPACE.md)

`agent/dev`와 그 화면 자산은 기존 회귀 테스트용으로만 보존했다. FastAPI는 이를 import하거나 서비스하지 않는다. 기존 Case API의 수기·필드·조건 저장은 실제 agent 자동 검사로 연결한다. 확인된 기본급 산술만 지원하며 표준 임금 조건을 임의 생성하지 않는다. 새 비교 묶음의 명시적 확인 흐름도 유지한다. [프론트 계약 복구·연동 정책](docs/FRONTEND_CONTRACT_FIXES.md)을 참고한다.
