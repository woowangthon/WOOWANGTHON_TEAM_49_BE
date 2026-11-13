# 최종 통합 안정화 검증

## 수정 원인과 동작

운영 DB에는 접근하지 않았다. Case 6의 실제 snapshot/job 내용을 확보하지 못했으므로 운영 STALE의 단일 원인을 확정하지 않는다. 아래 내용은 SQLite, TestClient, 실제 Agent 계산 경로로 재현한 코드 문제와 수정 결과다.

| 경계 | 이전 동작 | 수정 동작 |
| --- | --- | --- |
| 반복 재계산 POST | 같은 입력의 활성 job도 SUPERSEDED 처리하고 새 job 생성 | 현재 입력의 활성 job이 요청 검사를 포함하면 재사용. 입력 변경 시 기존 대체 정책 유지 |
| 과거 실패 작업 | 이전 inputVersion의 FAILED job이 현재 검사 상태를 FAILED로 표시 | 현재 inputVersion의 관련 job으로만 최신성 보정. 과거 오류는 latestJob에 보존 |
| snapshot 준비 | 실행마다 compat 그룹 version/updated_at 증가 | 입력 변경은 기존 schedule에서 버전 증가. 준비 자체는 같은 입력 버전을 유지 |
| 기본 비교 그룹 | needs_review 여부와 실제 입력 경로가 fingerprint에 반영되지 않음 | CONFIRMED_GROUP/CASE_FIELDS 구분. 재확인 필요한 그룹의 과거 입력을 현재 분석 입력으로 해시하지 않음 |
| 실행 도중 비교 기준 변경 | inputVersion이 같으면 완료 상태가 성공/부분완료로 남을 수 있음 | 게시 시 fingerprint 검증 유지, 완료 시에도 요청 검사 fingerprint가 바뀌면 SUPERSEDED |
| 중복 거래 제외 | 매핑 단계에서 중복이 있으면 지급 범위 전체를 미확정 처리 | 중복 표시는 유지하고 R-06이 해당 거래만 제외. 정상 거래 합계 허용 |
| 화면용 은행/현금 합계 | Case 배분 목록 전체 합계 | 분석에 고정된 transaction_allocations의 연결·확인·중복 제외 조건으로 합계 |

## 검사별 fingerprint

- calculation: 계약·근태와 급여 계산 조건 유지. 계산에 사용하는 comparison_basis_confirmed 누락 보완.
- comparison: 지급·공제 상세 범위 확인은 R-01/R-02/R-07 입력이 아니므로 제거. 비교 기준 조건과 관련 명세서 값만 반영.
- payslipValidation: 지급/공제 상세, 총액, 실수령액, 원본 상세 범위 확인을 반영. 명세서 시급·근로시간의 무관한 변경은 제외.
- depositComparison: 명세서 실수령액과 지급 거래/배분 반영. 지급·공제 상세 변경으로 불필요하게 STALE이 되지 않음.
- 공통: 확정 그룹의 출처·주체 확인·증거·관련 조건 상태를 반영. 실행 중 생성되는 그룹 version은 의미 기반 fingerprint에 추가하지 않음.

fingerprint 정책은 legacy-agent-3으로 변경했다. 기존 snapshot을 수정하거나 강제로 CURRENT로 만들지 않는다. 기존 정책의 결과는 STALE이 될 수 있으며 현재 입력 재계산으로 새 snapshot이 게시된다. 같은 입력의 기존 fingerprint가 일치하는 검사는 불변 snapshot을 재사용한다. Case 전체 inputVersion이 달라도 해당 검사 입력이 같으면 CURRENT일 수 있다.

## 검토와 분석

- REVIEW_REQUIRED는 추출 처리 상태이며 Case별 필수 사용자 검토 완료를 의미하는 CONFIRMED와 공존한다. 자동 READY 변환을 추가하지 않았다. OpenAPI에 의미를 명시했다.
- 검토 완료 후 재계산·목록·상세·review 조회에서 CONFIRMED를 유지하는 회귀 검증을 유지했다. 선택 필드 미확인만으로 검토 완료를 해제하지 않는다.
- 기존 필수 필드 사용자 확인, 선택 정상 추출값의 extracted_fields 전달, SYSTEM_EXTRACTED_UNVERIFIED 출처, MONTHLY 분기, 비교 기준 확인 정책은 유지한다.
- R-03/R-04 원본 항목 범위 확인은 금액의 사용자 확인과 분리된다. 범위 미확인은 ITEM_SCOPE_INCOMPLETE로 유지한다. R-05 산술 검증과 reasonCodes는 기존 Agent 경로를 사용한다.
- 업로드만으로 입금 확정하지 않는다. 후보 → 출처 검증 등록 → 사용자 확인 → Case 배분 → R-06 흐름과 등록 중복·배분 초과·출처 해시 검증을 유지한다. selected 내부 동일 transaction_id 중복 차단도 유지한다.

## FE 계약

기존 응답 키와 타입은 유지하고 다음을 추가했다. docs/openapi.json을 재생성했다.

- results.versions 및 recompute-status.results의 검사별 참조: currentInputVersion, snapshotFingerprint, currentFingerprint, fingerprintMatches, latestJobId, latestJobStatus, latestJobErrorCode.
- 기존 snapshotId/version/inputVersion/createdAt으로 게시 snapshot과 현재 fingerprint를 구분할 수 있다. snapshot이 없으면 snapshotId와 snapshotFingerprint는 null이다. PARTIAL은 계산 완전성이고 CURRENT는 입력 최신성이므로 공존할 수 있다.
- GET `/cases/{case_id}/documents/{document_id}/statement-scope`: documentId, inputVersion, statementScopes, scopeStatus 반환. 읽기 전용.
- 기존 PUT 동일 경로: expectedInputVersion/paymentItems/deductionItems 계약 유지. 위 조회 상태를 응답에 추가하고 기존 recompute 정보를 유지.
- scopeStatus.paymentItems/deductionItems: UNCONFIRMED, CONFIRMED, NEEDS_RECONFIRMATION. 금액의 사용자 확인 사실을 생성하지 않는다.
- FE는 processingStatus만으로 필수 검토 완료를 판단하거나, PARTIAL/정보 부족만으로 재계산을 반복 요청하지 않아야 한다. freshness와 activeJob 상태로 작업 진행을 판단하고 reasonCodes로 부족한 사용자 입력을 안내한다.

## 운영 확인

1. 배포 후 Case 6의 currentInputVersion과 네 검사별 참조·fingerprint·관련 job 오류를 함께 확보한다. resultVersion은 snapshot 버전이므로 inputVersion과 숫자를 직접 비교하지 않는다.
2. 현재 expectedInputVersion으로 한 번 재계산한다. 409이면 입력을 다시 조회하고 새 버전으로 요청한다.
3. 완료 후 네 검사 fingerprintMatches와 freshness를 확인한다. PARTIAL이어도 현재 입력 분석이 게시되면 CURRENT여야 한다.
4. 기존 실패의 errorCode와 SUPERSEDED 작업을 구분한다. 현재 입력으로 FAILED인 경우 오류를 제거하거나 과거 결과를 최신으로 표시하지 않는다.
5. 명세서 상세 범위 확인과 입금 후보 등록/확인/배분은 별도 사용자 행동이다. 부족한 기준을 자동으로 확정하지 않는다.

## 검증

최종 전체 pytest 로그: `docs/stability-pytest.txt`. 유료 모델 호출 없이 fixture 및 실제 Agent 계산 경로로 실행한다.
Ruff 전체, compileall(app/agent/tests/scripts), git diff --check를 실행한다. 기존 테스트의 정적 snapshot 참조 비교는 새 실시간 진단 정보와 분리하여 불변성 검증을 유지했다. 비동기 worker 회귀 테스트는 보고서·문서 추출 단계 각각에 독립적인 대기 시간을 부여했다.

- 최종 전체 pytest: **837 passed, 8 skipped**, 520.75초. langsmith의 기존 DeprecationWarning 13개.
- 신규 안정화 회귀 7개와 중복 거래/정상 합계 투영 회귀 1개 추가. 기존 829개 통과 기준에서 8개 증가.
- 관련 검사는 초기 58개 중 진단 필드 추가에 따른 불변성 비교 2건과 공용 deadline을 사용하는 worker 테스트 1건이 실패했다. snapshot 정체성 비교와 비동기 단계별 deadline을 보완한 최종 전체 실행에서는 모두 통과했다.
- Ruff 전체: 통과. 기존 scripts/legacy 테스트의 미사용 이름·import 및 의도적 지연 import lint도 정리했다.
- Python compileall(app/agent/tests/scripts): 통과.
- git diff --check: 통과.
- 운영 DB·운영 Case 6 재실행: 미수행. 배포 후 위 절차로 확인 필요.
