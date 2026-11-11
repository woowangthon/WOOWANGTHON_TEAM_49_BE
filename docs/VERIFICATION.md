# 통합 검증 기록

## 2026-11-11 1차 검토 작업실 통합

Linux / CPython 3.14.10 / `uv.lock` 환경. 변경은 `devmerge/` 안으로 제한했다. 검토 작업실은 별도 HTTP 서버 없이 FastAPI→내부 Python 그래프로 호출하며 SQLite·공통 worker를 사용한다.

| 검사 | 실제 결과 |
|---|---|
| 전체 활성 테스트 | **733 passed, 9 skipped**, 13 warnings, 50.13초 |
| 신규 검토 통합 테스트 | **15개**. 합성 문서·고정 모델, 실제 DB/원문 판독/좌표/기존 계산·보고서 경로 |
| Ruff: app·agent·통합·유지된 백엔드 | **All checks passed** |
| OpenAPI 생성 | **98개 경로**, 새 검토 API 포함 |
| 변경 범위 | `devmerge/`만 변경. 상위 agent/README/pyproject/lock 변경 없음 |

```bash
python -m pytest -q -rs --disable-warnings --maxfail=2 --basetemp=.pytest-verified-review
python -m ruff check app agent tests/integration tests/backend/test_retained.py
git diff --check
```

검토 초안·원문·확정·이력 보존, 혼합 PDF의 비중첩 구간, 실제 필드 좌표와 페이지 PNG, 같은 원본 재등록 시 중복 호출 방지, 오래된 revision/다른 사용자 접근 거절, 이미지 재검증의 미확정 후보 보존, 충돌의 개별 확인, 처리 중 취소·삭제·계정 철회·원본 변조의 게시 차단, 사용자 수정과 삭제값 보존, 중단 후 성공 응답 재사용을 검증했다. 원본 PNG→파생 PDF→기존 PDF/ZIP의 원본 바이트·source_manifest 보존도 검증했다.

검토 완료→기존 run_task→명세서 R-05 일치와 조건 부족 유지, 사용자가 명시한 표준 월급 조건의 총액 **3,272,727원**, 기존 사례 연결, 혼합 PDF의 서로 다른 입금 구간 등록과 동일 원문 행 중복 거절을 확인했다. 거래 원장·배분은 기존 경로를 사용한다.

9 skip은 미배포 문서 코퍼스 8개와 Windows 전용 소켓 옵션 1개다. 성공으로 세지 않는다. 13 warnings는 기존 LangSmith의 Python 3.14 호환 경고다. 처음 전체 실행에서 기존 합성 스캔 자료의 Windows 글꼴 경로 의존을 발견하여 포함된 NotoSansKR 글꼴을 사용하도록 수정한 후 위 전체 검증을 통과했다.

실제 OpenAI 유료 호출·스캔 OCR 정확도·Next.js 화면·다중 프로세스 배포는 검증하지 않았다. GitHub 푸시 여부는 로컬 검사 통과와 별개다. 이전 날짜의 결과는 아래에 이력으로 보존한다.

기록 갱신일: 2026-11-10 (Asia/Seoul). 기준 문서는 루트 기획명세서 v1.0와 원본 agent README 두 개다. `REQUIREMENTS_MATRIX.md`는 기획과 agent 지원 기능을 분리해 판정한다. 실행 결과는 날짜와 검증 범위를 구분한다.

## 2026-11-10 불필요 파일 정리 후 전체 검증

사용하지 않는 구 보고서 구현·라우터와 요청 모델, 최초 복사 스크립트를 제거했다. 개발 캐시·테스트 임시 산출물·중복 중간 로그를 정리하고, 테스트용 agent 개발 도구·폰트와 라이선스는 유지했다. 원본 보존 검증 결과는 이 문서에 요약하고, 상세 해시 파일은 이후 문서 정리 과정에서 제거했다.

| 검사 | 결과 |
|---|---|
| 전체 활성 테스트: 통합·유지된 백엔드·agent | **719 passed, 8 skipped**, 13 warnings, 100.04초 |
| Ruff: app·agent·통합 테스트·유지된 백엔드 | **All checks passed** |
| 원본 보존 | **255개 모두 동일** |

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --basetemp=.pytest-cleanup-20261110
.\.venv\Scripts\python.exe -B -m ruff check --no-cache app agent tests/integration tests/backend/test_retained.py
```

실행 후 테스트 임시 폴더도 삭제했다. 로컬 실행 환경 `.venv`는 유지하며 커밋 대상에서 제외한다. 브랜치 생성과 커밋은 수행하지 않았다.

## 2026-11-10 필수 수정 검증

Windows / Python3.14.4 / 통합 .venv / 잠금 의존성으로 실행했다. 원본 폴더에서 실행하거나 원본을 import하지 않았다.

| 검사 | 결과 | 범위/근거 |
|---|---|---|
| 필수 수정 후 통합·유지된 백엔드 회귀 | **89 passed**, 13 warnings, 73.36초 | 통합 테스트와 유지된 백엔드 테스트 |
| Ruff | **All checks passed** | 변경한 app 파일 5개와 새 회귀 테스트 3개 |
| 원본 보존 재확인 | **255개 모두 동일** | 원본의 추가·삭제·내용 변경 없음 |

이 결과는 필수 수정에 대한 회귀 검증이며 전체 agent 테스트를 포함하는 전체 실행 결과가 아니다. 아래 690 passed, 8 skipped는 2026-11-09 코드에 대한 기록이다. 외부 모델은 테스트 대역을 사용했고 실제 유료 모델을 호출하지 않았다.

## 2026-11-09 검증 이력

| 검사 | 당시 결과 | 보존 근거 |
|---|---|---|
| 기존 agent 회귀 | 630 passed, 8 skipped | 날짜별 요약 |
| 실제 DB·파일 runtime 통합 | 25 passed | 날짜별 요약 |
| 유지된 기존 백엔드 | 10 passed | 날짜별 요약 |
| 당시 전체 활성 테스트 | **690 passed, 8 skipped**, 13 warnings, 70.65초 | final-complete-pytest.txt |
| 전체 실행 이후 API 모듈 재검증 | **3 passed**, 13 warnings, 5.02초 | 날짜별 요약 |
| Ruff: app·agent·통합 테스트·유지된 백엔드 | **All checks passed** | 날짜별 요약 |
| 변경 전 기존 백엔드 계약 | maxfail10에서 **10 failed, 1 passed**로 중단 | backend-regression-original.txt |

당시 전체 실행에는 위 항목과 mapping/후보확인/API·거래 출처·철회 회귀가 포함된다. 중복 합산하지 않는다. 8 skip은 복사하지 않은 비공개 코퍼스 의존 테스트이며 성공이 아니다. 13 warnings는 langsmith의 Python3.14 deprecated asyncio.iscoroutinefunction 사용이다. 전체 수집 이후 추가된 API assertion(사례 지급일 변경→묶음 버전 증가·재확인→이전 버전409)은 같은 날 API 모듈 재검증으로 확인했다.

당시 실행 명령:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --disable-warnings --tb=short --basetemp=.pytest-final-complete
.\.venv\Scripts\python.exe -m pytest tests/integration/test_api_contract.py -q --disable-warnings --tb=short --basetemp=.pytest-final-api-latest
.\.venv\Scripts\python.exe -m ruff check app agent tests/integration tests/backend/test_retained.py
```

처음 제한 환경에서는 Windows asyncio의 `socket._fallback_socketpair → accept`가 테스트 본문 전에 멈췄다. faulthandler_timeout=20으로 확인하고 중단했다. 동일 테스트를 로컬 소켓 허용 환경에서 실행한 위 결과만 통과로 기록했다. 제품 코드에 socket 우회 monkeypatch는 넣지 않았다.

같은 날 중간 실행은 679통과/8skip, 범위별 실행은 125통과/1실패였다. 실패 원인은 문서 읽기/A 이후 버전 무효화가 추가되었는데 교체 테스트가 expectedVersion=1을 고정한 것이었다. 현재 그룹 버전을 조회하도록 수정한 뒤 당시 전체690통과로 재검증했다. 중복 중간 로그는 정리하고 결과 수치는 이 날짜별 요약에 남겼다.

## 실제 경로와 대역 경계

`tests/integration/test_runtime_db.py`는 실제 SQLite·임시 업로드/산출물 파일·DatabaseAgentPorts·입력 변환·실제 agent.runner.run_task·임금 규칙·backend finalize·결과 조회를 통과한다. 대역은 외부 모델 invoke만 대신하며 운영 fake fallback은 없다. 디지털 PDF 읽기와 PDF/ZIP 생성은 실제 라이브러리 경로다. 이미지 테스트는 문자 인식만 명시적 recognizer로 대체하고 PNG→PDF 변환/원본 관계/hash 저장은 실제로 수행한다.

확인한 동작:

- PDF 읽기→OCR ID→A 후보→명시 확인 JSON→계산→저장/조회. 사례 없는 문서의 case_id=null 추출.
- 표준 시급2,270,000원·월급3,272,727원 작성자 정답. 임금 총액 불가 시에도 R-03/04/05 유지. snapshot/result hash 일치.
- 같은 사례의 대안 묶음 격리. 원본/후보/문서 확인/묶음 확인 분리. 재추출 값 보존/재확인, B 계보·A 불변·자동 확인 없음.
- 사례 배분을 두 묶음이 재사용해도 원금1회 소비. 초과/동시 배분 거절. 현금/미확인/중복은 확인 은행합계에 포함되지 않음.
- 입금 문서의 실제 읽기/A 후보→원장→R-06까지 행 ID·금액·날짜·은행 참조·근거를 보존한다. DOCUMENT라는 요청 값만으로 검증 출처가 되지 않는다. 송금자 불일치·같은 원본 행 중복을 거절하고 서로 다른 확인 은행 참조는 같은 날짜/금액이어도 별개로 대조한다. 원본 삭제는 연결된 사례와 배분된 사례의 합집합을 무효화하고 파생 거래를 삭제하되 수기 거래는 보존한다.
- 입력/묶음 변경 결과 stale 보존/latest 미갱신. 실제 agent 반환 후 취소·사례삭제·원본삭제·연결철회·소유권철회·실행권 상실은 확정 거절.
- 모델 호출 중 별도 DB writer 성공. 재시작 후 이전 실행권 무효·중복 결과 없음. 실패3회 예산 보존 후 재시도 추가 호출0.
- 과거 결과 PDF의2,270,000원·2026-04-01~04-30 확인; 현재 입력 기간5월과 혼합 없음. 연결철회 후 다운로드 차단.
- 로그인·권한·그룹 API·작업/결과 이력·설명·문의·채팅 미설정 오류·삭제·CORS/OpenAPI.
- 연결 해제 후 재연결해도 이전 결과/작업/파일 접근은 복원되지 않는다. CONTEXT 자료는 급여 확인값 근거가 될 수 없고 근무/휴게 구간은 context 역할을 유지한다. B의 같은 멱등키를 새로운 A 후보로 재지정할 수 없다.

기존 agent 테스트의 FakePorts/합성 공급자/OCR 성공만으로 운영 DB 통합을 주장하지 않는다. Next.js 화면·실제 외부 모델 정확도·실제 스캔 OCR 품질·모든 근무형태 법률 검증·다중 프로세스 배포는 미검증이다.

## 기존 백엔드 테스트 처리

먼저 이전 기대 계약을 실행해10실패/1통과를 기록했다. 실패는 HTTP analyze/chat 어댑터·기본 mock 성공·업로드 자동 AI·수기 저장 즉시 구 백엔드 계산 등을 기대했다. 사용자 요구와 충돌하는 계약이므로 운영 경로를 되돌리지 않았다.

복사본은 `tests/backend/legacy_contracts/original_*.py`에 보존했다. 파일명과 통합 위치의 conftest import만 변경했다. 기본 실행에서 skip/pass로 세지 않는다. 유지된 인증·소유권·동시 배분·입력 검증·DB migration/snapshot10개 함수는 test_retained.py가 원래 함수를 수집한다. 새 실행 계약은 실제 agent 통합 테스트로 대체했다. 미실행 legacy 나머지는 통과로 주장하지 않는다.

## 구조 검토

| 질문 | 판정/근거 |
|---|---|
| Next.js용 API 계약 | routers/integration.py·OpenAPI·API 테스트. 화면 미구현. |
| backend→agent 직접 함수 호출 | agent_service.py import/await run_task, 실제 DB 통합 테스트 |
| 별도 agent HTTP/AI_BASE_URL 불필요 | app·설정 예제 검색에 AI_BASE_URL/ai_base_url/8001 URL 없음. 외부 모델 HTTPS와 구분 |
| 인증·권한·저장·작업 backend 소유 | auth/security·integration_store·agent_ports·worker |
| 계산·대조 agent 실행 | 실제 standard rule bundle와 run_task 기대 금액 테스트 |
| 결과 ID/version/latest/API backend 소유 | finalize 새 트랜잭션과 결과 조회 테스트 |
| 운영 dev/고정 사용자 비의존 | app에서 agent.dev/owner-local/LocalFilePorts 참조 없음; 순수 검증 services 이동 |
| 원본 미참조 독립 실행 | 공통 import 구조와 아래 독립 복사 실행 기록 참조 |

## 미완료·미검증

- 지역 상담기관 목록/정기 갱신 미구현. 기존 전국 seed 최신성 재검증 없음.
- 전체 개인정보 사본/백업 영구 삭제와 완료 통보/기한, 운영 개인정보 고지·보관 정책 미완료. 서비스 접근 차단과 영구 삭제를 구분한다.
- 실제 OpenAI 추출/B/채팅 호출과 실제 스캔 OCR 품질 미검증. 운영 OCR 실행파일과 한국어 데이터는 별도로 준비해야 한다. 한글 정적 Noto Sans KR 폰트와 라이선스는 통합본에 포함되며 독립 복사본에서 PDF 생성까지 검증했다.

## 추가 검증 기록

- 이미지 원본 ZIP 회귀: 업로드 PNG와 archive 내부 PNG 바이트가 정확히 같고 source_manifest.json의 original_hash와 processing_pdf_hash 관계가 일치한다. 실제 보고서 생성 경로로 통과했다.
- 문서 교체 API 회귀: 묶음 confirmedInput 보존·needsReview=true·새 원본 선택, 공유 이전 원본은 다른 사례에서 접근 가능, 기존 DB의 사례 수정값은 새 문서에서 UNCONFIRMED로 보존됨을 검증했다.
- 원본 바이트를 모델 호출 중 변조하면 확정 단계에서 후보 저장을 거절한다. 보고서 폰트 오류로 보고서 작업이 실패해도 기존 확정 계산 결과는 보존된다.
- 2026-11-09 독립 실행: `tmp/devmerge-independent-20261109`에 devmerge 코드만 별도 복사하고 새 .venv/오프라인 잠금 의존성 설치. 실제 uvicorn 건강검사 direct/1.0.0, OpenAPI87개 경로, import 경로가 독립 복사본 내부임을 `independent-smoke.json`에 기록했다.
- 2026-11-09 독립 API3개 테스트 통과(6.04초): 실제 agent 결과·설명, 번들 폰트 PDF 생성·다운로드/삭제, 문의 편집·사례 변경 시 이전 버전409, 권한 및 채팅 오류 계약. 중간 로그 정리 후 결과를 이 요약에 보존했다.
- 원본 보존: 검증 당시 원본 255개 파일의 목록·내용 해시가 시작과 종료 시점에 동일했고, 추가·삭제·변경 파일이 없었다. 상세 해시 파일은 문서 정리 과정에서 제거했다.

## 합성 자료

comparison_four_documents.json은 synthetic:true와 가상 근로자/사업장·TEST 식별자를 명시한다. attendance_real_response/monthly_scan_response는 기존 합성 문서 모델 응답 회귀자료다. 비공개 원본 코퍼스·실제 사용자 문서·비밀값 파일은 복사하지 않았다. API키/연락처 패턴 검색에서 실제 비밀값을 발견하지 않았다.
