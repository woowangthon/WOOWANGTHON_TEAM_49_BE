# 프론트 API 계약·에이전트 통합 검증

검증일: 2026-11-11 (Asia/Seoul). 코드 기준: `9f24e26`, 실제 통합 저장소 `WOOWANGTHON_TEAM_49_BE` 루트.

**현재 통합 구현 자체의 회귀 테스트와 새 실행 경로는 통과한다. 그러나 첨부한 이전 프론트 연동 명세와 호환되지 않으므로, 이 문서를 보고 만든 프론트를 그대로 연결해 정상 동작한다고 판단할 수 없다.** 단순 기능 추가 외에 실행 순서·입력·출력·버전·보고서 경로가 변경됐다.

## 1. 기준과 검사 범위

- 첨부 1: v0.4 원문과 B-01~B-92 상세 작업을 포함한 v0.5 **제안** 문서. 미합의 필드/상태는 확정된 서버 버그라고 단정하지 않는다.
- 첨부 2: 통합 전 백엔드의 **95개 HTTP 작업**을 전제로 한 프론트 연동 명세. 저장 후 자동 재계산·독립 결과 버전·기존 보고서 API가 기준이다.
- 자료의 제품/설계 내용은 비교 기준으로 읽었다. 첨부 문서 안의 제안·미확정 사항을 새 구현 지시나 승인으로 취급하지 않았다.
- 전체 테스트, 정적 검사, 현재 OpenAPI와 저장된 OpenAPI 비교, 92개 method/path 및 인증 상태 전수 탐침, 주요 DTO/업무 시나리오, 실제 Docker TCP 통신, 백엔드→agent.runner.run_task 호출 추적을 수행했다.
- 모든 계정·파일·DB는 임시 합성 자료다. 실행 중인 사용자 DB나 원문은 사용하지 않았다. 이 작업은 검증 코드와 기록을 추가했으며 앱 런타임은 변경하지 않았다.

## 2. 실행 결과

| 검사 | 결과 | 해석 |
| --- | --- | --- |
| 전체 활성 회귀 | **733 passed, 9 skipped**, 13 warnings, 30.60초 | 현재 구현의 테스트 계약 통과; 이전 프론트 계약 준수를 뜻하지 않음 |
| Ruff / 컴파일 | 통과 | app·agent·통합·유지된 백엔드와 감사 스크립트 |
| 현재 OpenAPI | **98개 path / 133개 HTTP 작업** | 저장된 docs/openapi.json의 method/path 집합과 일치 |
| B장 92개 method/path | **87개 등록 / 5개 누락** | 누락은 기존 보고서 API 5개 |
| 실제 TCP 92개 탐침 | 401×82, 422×4, 200×1, 404×5 | 인증 없이 경로/검증 존재를 확인; 87개 성공 DTO를 검증했다는 뜻이 아님 |
| 비교/기능 점검 | **38개 항목: 현재 경로 통과 10개, 계약 차이 28개** | 28개는 검사 항목 수이며 독립 버그 수가 아님. 이전 계약 24항목, 제안 계약 4항목 |
| 실제 HTTP E2E | 통과 | 가입·로그인·업로드·검토 작업실·판독·PNG·확인 입력·agent 계산·설명·PDF·문의 |
| agent 직접 호출 추적 | 추출/계산/설명/보고서 모두 completed | 외부 공급자만 명시적 테스트 대역; 실제 runner·규칙·DB·파일 경로 사용 |
| 실제 TCP CORS/인증 | 통과 | 허용 origin preflight, Bearer, HttpOnly/non-Secure HTTP 쿠키·동일 origin refresh |
| 모델 키 없는 SSE | 예상 실패 처리 통과 | HTTP 200 스트림에 message_start→error(MODEL_NOT_CONFIGURED), 질문 COMPLETED/답변 FAILED 저장 |

9 skip은 비공개/선택 코퍼스 8개와 Windows 전용 소켓 옵션 1개다. 13 warnings는 LangSmith의 Python 3.14 asyncio deprecation이다. `legacy_contracts/original_*.py` 보관 파일 전체는 기본 pytest 수집 대상이 아니며 유지된 함수만 test_retained.py에서 실행한다. 이들 보관 파일까지 모두 통과했다고 주장하지 않는다. 이전 프론트 계약은 별도 감사로 비교했다. 원시 결과는 [pytest.txt](contract-audit/pytest.txt), [findings.json](contract-audit/findings.json), [HTTP 흐름](contract-audit/http-smoke.json), [TCP 탐침](contract-audit/http-transport.json)에 있다.

## 3. 프론트 연결을 막는 변경

여기서 P0는 **이전 명세를 따르는 프론트의 주요 흐름 차단**, P1은 데이터/실행 의미 또는 검증 불일치, P2는 제안 필드·예제 문제다. 보안 취약도 점수가 아니다.

### P0 — 저장 후 자동 실행이 사라짐

Case 생성, 수기 명세서 등록, 조건 저장의 응답이 `recompute.scheduled=false`, `jobId=null`, `action=CONFIRM_COMPARISON_GROUP`이다. `schedule()`은 입력·묶음 버전과 재확인 상태만 바꾸며 작업을 생성하지 않는다. 이전 프론트가 저장 후 recompute-status를 polling해도 자동 계산 완료를 받지 못한다.

첨부 명세 그대로 수기 PAYSLIP을 등록한 다음 `POST /cases/{id}/payslip-validations`를 호출하면 **409 GROUP_RECONFIRMATION_REQUIRED**다. 수기·조건 행은 저장되지만 새 비교 묶음의 `confirmed-input`으로 자동 변환되지 않는다. 확인된 입력을 새 API로 별도 저장해야 실제 agent가 실행된다.

근거: [engine.schedule](../app/engine.py), [analysis 라우터](../app/routers/analysis.py), [재현 JSON](contract-audit/findings.json).

### P0 — 기존 필드 수정 경로가 추출 후보를 거절

추출 후보에서 생성된 필드에 기존 PATCH `/cases/{c}/documents/{d}/fields/{f}`로 수정/확인하면 숫자·문자열 값 모두 **409 USE_CANDIDATE_REVIEW_ENDPOINT**다. 새 입력은 `POST /documents/{id}/candidate-reviews`로 확인해야 한다. 감사에서 이 새 경로의 확인은 CONFIRMED로 통과했다.

필드 키/타입도 `grossPay: number, MONEY_KRW` 대신 `gross: "2300000", TEXT`다. 단순히 경로만 바꾸는 것으로 기존 프론트 타입과 의미가 일치하지 않는다. 로컬 읽기 완료 상태 **READ_READY**도 이전 문서의 processingStatus enum에 없다. 실제 자동 업로드는 READ_READY, fields=[]에서 멈추며 추출 A는 명시적으로 요청해야 한다.

근거: [documents.update_field](../app/routers/documents.py), [후보 저장](../app/agent_service.py).

### P0 — 통합 결과 DTO가 이전 결과 화면과 다름

실제 agent가 명세서 산술 차액 10,000원을 계산한 뒤에도 기존 GET `/cases/{id}/results`는:

- `summary.counts` 없음.
- `items`가 `id/title/calculatedValue/documentValue/sources`를 가진 ResultItem이 아니라 `rule_id/lhs/rhs/delta/evidence_refs` 등의 agent 비교 결과.
- 항목 상태는 `difference`, `insufficient` 등 소문자이고 금액은 Decimal 문자열.
- `payslipValidation=null`, `depositComparison=null`; 유효한 비교 결과를 이전 상세 섹션으로 변환하지 않음.
- snapshotId가 정수 대신 문자열이고 네 검사 종류에 같은 통합 결과 참조를 반환.
- 명세서 이력도 `validationId/paymentTotal` 구조 대신 통합 agent 결과를 반환.

GET `/recompute-status`도 `results`가 없고 `latestJob`는 `jobId` 대신 `id` 등 snake_case agent 작업 DTO를 반환한다. 기존 results/readiness/recompute 타입을 그대로 쓸 수 없다.

근거: [engine.results/latest](../app/engine.py), [analysis.recompute_status/snapshot_list](../app/routers/analysis.py).

### P0 — 기존 보고서 5개 API가 없음

다음 method/path는 OpenAPI에 없고 실제 TCP에서도 404다.

| Method | Path |
| --- | --- |
| POST | `/cases/{caseId}/reports` |
| GET | `/cases/{caseId}/reports` |
| GET | `/cases/{caseId}/reports/{reportId}` |
| GET | `/cases/{caseId}/reports/{reportId}/pdf` |
| GET | `/cases/{caseId}/reports/{reportId}/originals` |

새 경로는 `POST /agent-results/{resultId}/explanations` → 작업 완료 → `POST /agent-results/{resultId}/reports` → 작업 output.artifacts → `GET /artifacts/{artifactId}`다. PDF/ZIP 실제 생성·다운로드는 성공했고 ZIP의 summary.pdf와 단독 PDF 바이트도 같았다. 그러나 기존 Case 단위 보고서 이력 API의 동일한 대체 경로가 생긴 것은 아니다.

## 4. 추가 실행·업무 의미 차이

| 항목 | 관찰 | 영향/조치 |
| --- | --- | --- |
| **PDF 페이지 제한 불일치** | 업로드는 50페이지 허용, AgentSettings.max_pages=20. 실제 21페이지 PDF가 202 접수 후 FAILED/DOCUMENT_UNREADABLE | 앱과 agent의 허용 범위를 하나로 맞춰야 함. 단순 새 DTO 문제가 아닌 실제 제한 불일치 |
| 독립 버전·영향 범위 | 배분만 바꿔도 계산 freshness가 CURRENT→STALE. latest(kind)가 kind를 분리하지 않음 | 입금 변경은 입금 결과만 바뀐다는 이전 정책 미충족 |
| 검사별 실행 | `/payslip-validations`, `/calculations` 모두 analyze_case를 실행. checks는 응답에만 있고 agent payload에는 없음 | 이전 검사별 독립 실행/버전 계약과 다름. agent는 새 통합 분석 계약대로 동작 |
| readiness | mode=AGENT_SUPPORTED_SCOPE, 모든 검사에 같은 available 판정, missingInputs=[] | FULL/PARTIAL/NONE 및 기능별 부족 입력 UI와 불일치 |
| 회사 문의 선택 | 알 수 없는 resultItemIds·낡은 basedOnResultVersions를 보내도 200 | 이전 호환 라우터가 body를 사용하지 않음. 새 result 기반 문의와 구분해 검증 필요 |
| 현금 합산 | 현재 R-06은 확인된 중복 없는 은행 배분 중심, 현금은 별도 메모 | 이전 linkedPaymentTotal=은행+현금 표시/대조와 의미를 합의해야 함. 기존 회귀 테스트는 현재 은행 기준 정책을 검증 |
| 초기 업로드 | READ_READY까지 로컬 판독만 수행 | 이전 UPLOADED→READY 추출/확인 화면 전환에 새 단계 추가 필요 |

PDF 문제의 근거는 [업로드 검사](../app/routers/documents.py), [agent max_pages](../agent/config.py), [판독 검사](../agent/services/document_ocr.py)다. 실제 HTTP 재현은 [http-transport.json](contract-audit/http-transport.json)의 accepted_21_page_PDF에 있다. 이 파일은 빈 문서가 아닌 21페이지 디지털 텍스트 PDF이며, 별도의 진단용 설정 사본에서 max_pages만 50으로 바꾸면 같은 바이트가 21개 pdf_text 페이지로 정상 판독됨을 확인했다. 운영 설정은 변경하지 않았다.

## 5. 제안 계약과 기존 문서 자체의 차이

다음은 v0.5에서 제안된 부분이므로 현재 서버의 확정 회귀라고 세지 않았다. 다만 프론트가 제안 DTO를 이미 구현했다면 대응이 필요하다.

- 근무지 POST가 전체 Workplace가 아니라 `{workplaceId}`만 반환한다. 이는 이전 v0.4 본문/연동 명세와는 일치하고 v0.5 제안과 다르다.
- 제안 recompute의 `{checks,reason,expectedInputVersion}`는 422. 현재 Execute 입력은 confirmed/expectedInputVersion이다.
- 제안 `clientMessageId`를 chat POST에 넣으면 422. 채팅 중복 요청 보장은 이전 연동 명세에서도 지원하지 않는다고 적혀 있다.
- chat 상세는 `{session, messages:Page}`가 아니라 평탄한 세션 필드와 messages 배열이다. 페이지네이션 제안은 미적용이다.

첨부 2의 curl/JS 예제에는 `Bearer$TOKEN`, ``Bearer${accessToken}``처럼 **Bearer 뒤 공백 누락**이 있다. 실제 요청은 `Authorization: Bearer <token>`이어야 한다. 공백 없는 헤더가 401임을 실제 HTTP에서 확인했다. v0.5의 CANCELED와 현재/이전 연동 명세의 CANCELLED도 구분해야 한다.

## 6. 백엔드가 agent를 올바르게 부르는가

**현재 새 내부 계약 기준으로는 검증 범위에서 올바르게 호출·저장된다.** 이전 프론트 입력을 그대로 새 입력으로 이어주는 호환 계층은 별개로 부족하다.

감사에서 실제 `app.agent_service.run_task`를 관찰용 wrapper로 감싸 원래 runner를 그대로 실행했다.

| task_kind | 실제 입력 타입 | 결과 |
| --- | --- | --- |
| extract_document | ExtractionInput | completed, 후보 확인까지 통과 |
| analyze_case | AnalysisJobInput | completed, R-05=차이/10,000원 |
| explain_result | ExplanationInput | completed |
| build_report | ReportInput | completed, PDF/ZIP 바이트 검사 통과 |

확인한 처리 경계:

1. 백엔드가 사용자·Case·묶음·문서 연결 권한과 버전을 확인하고 입력을 고정한다.
2. `AgentTask(context, payload)`와 DB/File ports를 만들어 **직접 await run_task(...)**한다. 별도 agent HTTP 서버는 없다.
3. agent 규칙이 계산하고 backend confirm_result/finalize가 hash·입력 계보·실행권·취소·권한을 다시 확인해 저장한다.
4. 읽기/모델 대기 동안 write 트랜잭션을 잡지 않는지, 실행 후 입력 변경/삭제/철회가 최신 결과 게시를 막는지 기존 실제 DB 회귀가 통과했다.
5. 추출 재시도 시 확정값/이력 보존, 다른 묶음 격리, 같은 배분 이중 소비 방지, 원본 변조/삭제·연결 철회 후 과거 산출물 접근 차단, 보고서의 당시 입력 고정도 전체 테스트에 포함됐다.

표준 시급/월급의 작성자 정답 사례(2,270,000원 / 3,272,727원)도 기존 실제 agent·DB 통합 테스트가 통과했다. 이는 지원 사례 산술 검증이며 광범위한 법률 검증 완료를 뜻하지 않는다.

새 검토 작업실은 `agent.review.pipeline.run_pipeline`로 판독·분류·후보 처리를 하고, 완료 연결 시 기존 비교 묶음/runner 분석 경로를 사용한다. 자유 채팅은 `AIAdapter.chat`에서 모델을 직접 호출한다. 이 두 경로가 모두 run_task를 호출하지 않는다는 사실만으로 잘못된 호출이라고 판단하지 않았다. 각각 검토 통합 테스트와 채팅 문맥·출처 테스트를 확인했다.

## 7. 통신과 외부 공급자 검증의 경계

- Docker의 실제 TCP HTTP에서 CORS·Bearer·쿠키 refresh·업로드·worker·검토 페이지 PNG·계산·PDF 다운로드를 확인했다.
- TestClient는 모델/채팅 성공 응답만 명시적으로 대역 처리했다. 계산·검증·DB·파일·runner를 성공값으로 바꾸지 않았다.
- 실제 OpenAI 추출/B/이미지 검증/자유 채팅의 유료 호출은 **0회**다. 모델 ID 접근권한·실제 모델 응답 품질·스캔 OCR 정확도를 검증했다고 말할 수 없다.
- 실제 EC2/프론트 배포 주소가 제공되지 않아 로컬 Docker에서 확인했다. 브라우저의 서로 다른 사이트 쿠키·혼합 콘텐츠·프론트 화면·외부 SSE 구간은 미검증이다.
- 현재 OpenAPI의 method/path는 저장본과 일치하지만 **103개 JSON 성공 응답의 schema가 비어 있다.** Swagger에 경로가 있다는 것만으로 응답 DTO나 프론트 생성 타입이 보장되지 않는다.
- 92개 전체는 route/auth probe이며, 모든 성공 DTO·모든 query·DELETE 조건·모든 오류 조합을 새로 전수 검증한 것은 아니다. 주요 흐름 재현과 733개 기존 회귀의 범위를 구분했다.

## 8. 재현과 우선 정리

```bash
uv sync --locked
uv run pytest -q -rs --disable-warnings --tb=short
uv run python -m scripts.audit_frontend_contract --strict
```

마지막 명령은 현재 **의도적으로 종료 코드 1**이다. 감사 스크립트의 실행 오류가 아니라 5개 경로 누락과 28개 비교 항목 불일치를 발견했기 때문이다. --strict를 빼면 결과 JSON을 수집하고 종료 코드 0을 반환하므로 이를 계약 통과로 해석하지 않는다. 테스트 대역 모듈을 사용하는 개발 검증 명령이며 운영 컨테이너의 모델 fallback이 아니다.

전체 method/path는 [endpoint-matrix.md](contract-audit/endpoint-matrix.md), 요청/응답 재현은 [findings.json](contract-audit/findings.json)을 본다.

우선 정리할 대상은 (1) 프론트 입력·확인·실행 순서, (2) 결과/작업 DTO, (3) 보고서 생성·이력·다운로드, (4) 독립 최신성/부분 실행 정책, (5) 페이지 한도다. 새 기능을 유지하려면 기존 프론트용 adapter를 만들거나 프론트 명세·타입·호출 순서를 새 API로 함께 바꿔야 한다. 부분적으로 경로만 살려 놓고 이전 계약과 호환된다고 전달하면 안 된다.
