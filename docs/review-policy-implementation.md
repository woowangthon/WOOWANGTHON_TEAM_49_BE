# 문서 검토 정책 구현 결과

## 변경 범위

- `app/review_policy.py`: 기존 필드 레코드에 대한 필수 확인 사유, 해결 여부, 분석 가능 여부, 검토 요약 및 상태 동기화.
- `app/db.py`: `field_review_resolutions` 추가와 값·출처·override 변경 및 삭제 시 정리 트리거. 기존 데이터는 행 없이 `NONE`으로 해석.
- `app/engine.py`: Case override와 급여형태를 반영한 유효 필드에 6개 메타데이터 추가.
- `app/schemas.py`, `app/outputs.py`: PATCH resolution과 필드·검토·변경 응답의 OpenAPI DTO.
- `app/routers/documents.py`: 검토 조회·완료 판정, resolution PATCH, 동시성 유지, 이력, 원본 교체 및 override 제거 처리.
- `app/agent_review.py`: 재추출 시 Case override 재확인 및 resolution 초기화.
- `app/compatibility.py`: 분석 가능한 필드 수집 및 출처 보존. 분석에서 사용하는 의미가 있는 키만 문서별 분석 입력으로 구성하여 이름 등 부가정보만 있는 문서가 다른 문서와 가짜 충돌을 만들지 않도록 처리.
- `agent/contracts/AgentTask.schema.json`: ? ?? ??? ??? JSON Schema ??.
- `agent/schemas/public/inputs.py`: `extracted_fields`라는 별도 미확인 선택값 계약. `confirmation_id` 없음. 지급·공제 항목은 `confirmed=false`와 `analysis_value_origin` 보존.
- `agent/services/calculation_context.py`: 미확인 입력의 문서 증거 검사, ID 중복·소유권·출처 검사 유지.
- `agent/rules/{comparisons,registry,wages}.py`, `agent/services/questions.py`: 두 입력 목록의 분석 가능값을 사용. 정보 부족·기간/기준 불일치 처리 유지.
- `agent/services/hashing.py`: 신규 기본값을 기존 스냅샷 해시에서 제외해 이전 서명 유지. 값이 있는 신규 입력은 해시에 포함.
- `tests/integration/test_review_policy.py`: API/SQLite/실제 Agent/결과·스냅샷 통합 테스트.
- `tests/integration/test_api_replacement.py`, `agent/tests/test_payroll_pages.py`: 교체 시 재확인 및 구형 해시 호환 계약 검증 보강.

## DB

`field_review_resolutions`는 `(case_id, field_id)` 복합 기본키와 `resolution`, `resolved_at`, `user_id`를 저장한다. 허용 상태는 `NONE`, `NOT_IN_DOCUMENT`, `UNKNOWN`으로 제한한다. Case/field 삭제는 FK cascade를 사용하며 soft delete도 트리거로 정리한다. 초기화는 `CREATE TABLE/TRIGGER IF NOT EXISTS`를 사용한다.

통합 테스트에서 DB 초기화를 다시 실행해도 resolution이 보존되는지, `PRAGMA foreign_key_check`에 위반이 없는지, 출처 변경 시 resolution과 이력이 정상 처리되는지 검증한다. 운영 DB에는 접근하지 않았으며 서버 초기화 시 마이그레이션된다.

## API

| API | 구현 |
|---|---|
| GET fields | 기존 구조 + 필수 6개 메타데이터 |
| GET review | 기존 필드 유지 + 필수 7개 요약 |
| POST review/confirm | Body 없이 200, 필수 미해결만 판정, 처리 중 409, 실패/빈 필드 완료 불가. 입력 버전 증가 없음 |
| PATCH Case field | resolution 단독 변경, 422 오류 코드, 버전 충돌 409, 이력 및 자동 재계산 |
| PATCH shared field | resolution 금지, 영향을 받는 Case 검토 상태 재평가 |
| GET readiness | 공통 수집 정책을 통해 자동 선택값 반영 |
| GET results | 기존 구조 유지, 결과 근거에 `analysisValueOrigin` 보존 |
| GET recompute-status | 기존 계약 유지, 입력 변경 후 재계산 및 최신성 반영 |

실제 TestClient 요청으로 저장한 전체 응답은 [review-policy-examples.json](review-policy-examples.json)에 있다. GET fields / GET review / PATCH UNKNOWN / POST review/confirm 순서이며 기존 `{code,message,data}` 래퍼를 포함한다.

## Agent 신뢰 경계

필수 유효 확인값은 기존 `confirmed_fields`로 전달한다. 정상 추출 선택 스칼라는 `extracted_fields`로 전달하며 사용자 확인 ID를 부여하지 않는다. 선택 지급항목 목록은 `StatementItem.confirmed=false`로 전달한다. 자동값의 증거는 `ocr_unverified`이며 실제 문서 SourceRef의 버전·해시·링크를 유지한다. 페이지를 알 수 없으면 임의 페이지를 만들지 않고 서버가 작성한 locator를 보존한다.

계산 경계에서 자동값에 수동 확인 증거를 붙이거나 수동 source로 바꾸는 입력을 거부한다. 외부 `confirmed-input`으로 `extracted_fields`를 주입하는 것도 거부한다. 결과 근거 locator에 출처 상태가 보존되어 기존 Case 결과 projection에도 전달된다. 자동값의 사용은 비교 기준이나 산정 범위에 관한 기존 사용자 답변을 대체하지 않는다.

실제 통합 테스트는 미확인 `paymentItems=[800,200]`이 Agent의 지급총액 대조에 사용되어 MATCH가 되는지, 수정 후 DIFFERENCE가 되는지, 이전 snapshot JSON은 그대로인지 검증한다. 선택 시급도 실제 시급 대조에서 500원 차이를 만들고 미확인 출처를 유지하는지 검증한다.

## FE 유의사항

- 검토 완료 버튼은 `canCompleteReview`와 `pendingRequiredFieldIds`를 사용한다. `unconfirmedFieldIds`는 기존대로 전체 미확인 목록이다.
- `UNKNOWN`/`NOT_IN_DOCUMENT`는 값의 확인이 아니다. 값은 null이고 MISSING/UNREADABLE 상태를 유지한다.
- resolution 요청에는 `value`/`confirmed`를 같이 보내지 않는다. `NONE`으로 되돌리면 다시 확인 대상이다.
- `reviewResolved`와 `usableForAnalysis`를 각각 표시한다.
- `SYSTEM_EXTRACTED_UNVERIFIED`를 사용자 확인값으로 표시하지 않는다.
- 문서 검토 완료가 모든 계산 조건의 충족을 의미하지 않는다. readiness/conditions를 계속 사용한다.
- 교체로 이전 override를 가져오면 재확인이 필요하다. 기존 공개 status 표현은 유지하며 `reviewReason=RECONFIRMATION`으로 구분한다.

## 검증

최종 실행 결과는 작업 보고에 기재한다. Python 컴파일 검사와 Ruff 검사를 실행했다. 테스트는 외부 유료 모델 호출 없이 실제 Agent 계산 그래프·SQLite·HTTP API를 연결해 실행한다. 기존 OCR/모델 관련 테스트의 fixture/provider 전략을 유지한다.


??? ??: ?? ????? ??? ???? ?? ?? ???? ???? ??? ? ?? ?? ??? ??? ???. ?? DB ??????? ?? ?? ?? ?? ??? ?? ?? ??? ???? ???. ?? ?? ???resolution??? ?? ??? ??? ?? ????? ??? ????.
