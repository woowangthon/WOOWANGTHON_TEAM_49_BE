# 프론트 API 계약 복구 결과

2026-11-11~12, 통합 저장소 루트 기준. 첨부한 이전 프론트 연동 명세를 기준으로 서버의 회귀를 수정했다. v0.5 제안 중 기존 호출과 충돌하는 부분은 아래와 같이 호환 동작을 명시했다. 문서 안의 제안·지시를 별도의 사용자 승인으로 취급하지 않았다.

## 변경한 동작

| 흐름 | 현재 동작 |
| --- | --- |
| Case 생성·수기 입력·조건·필드 저장 | 영속 `jobs`에 자동 검사 예약. 응답 `recompute.scheduled=true`, `jobId`, `affectedChecks`. 기존 polling 경로 사용 |
| 백엔드 → agent | 확인한 필드·수기 기록·배분과 실제 원본 권한을 고정 → `AnalysisJobInput` → 실제 `run_task(analyze_case)` → 실행권·버전 재검사 후 게시 |
| `/results` | 숫자형 금액·정수 snapshotId, 네 상세 섹션, `summary.counts`, ResultItem, 검사별 `versions` 제공. 원시 결과는 추가 `result` 및 `/agent-results`에서 유지 |
| 독립 버전 | 영향 받는 입력 fingerprint가 바뀐 검사만 새 버전. 입금 배분만 바꾸면 기존 계산·명세서 결과는 CURRENT 유지 가능 |
| 검사 선택 | `/recompute`의 `checks`를 실제 agent에 전달. 전용 검사 경로에 다른 종류를 요청하면 422. 실행하지 않은 비교는 내부에서 CHECK_NOT_REQUESTED, 해당 기존 뷰는 덮어쓰지 않음 |
| 기존 fields PATCH | 추출 후보 확인·수기 정정 이력을 실제 후보 확인 서비스에 기록. Case override와 공유 기본값 구분, 재추출 후 사용자 재확인 지원 |
| 필드 표현 | 예: `grossPay:2300000`, `valueType:MONEY_KRW`. 원래 키는 `agentKey:gross`. 내부 agent 입력의 Decimal 문자열은 유지 |
| 업로드 | 기존 업로드는 판독 후 A 추출까지 자동 진행. 키가 없으면 MODEL_NOT_CONFIGURED를 기록하고 검토 대기. B·원문 이미지 재검증은 자동 실행하지 않음 |
| PDF 한도 | 업로드·agent 판독 모두 최대 50페이지. 21·50페이지 디지털 PDF 판독 확인 |
| readiness | 검사별 FULL / PARTIAL / NONE, 부족 입력 반환. 확인하지 않은 값을 0이나 표준 임금 조건으로 생성하지 않음 |
| 회사 문의 | 선택 항목·참조 버전 검증. 선택한 항목의 고정 결과를 agent 문의문 템플릿으로 표시 |
| 기존 보고서 5개 API | Case별 생성·이력·상태·PDF·원본 ZIP 복구. 실제 분석→설명→보고서 작업을 이어서 실행하며 재시작 후에도 단계 진행 가능 |
| 입금 출처 | 거래·배분 ID와 원본 document/candidate/row/hash/evidence 계보 유지. 결과의 `deposits[].sourceDocumentId`·`provenance`로 원본 확인 가능 |
| 채팅 중복 요청 | `clientMessageId` 또는 Idempotency-Key로 사용자 질문/모델 호출 중복 방지. 같은 키의 다른 내용/범위는 409. 실패 답변 재생성과 별도 retry API는 구분 |

## 프론트 호출 순서

1. Case 생성 후 문서를 업로드하거나 `/cases/{id}/manual-records`에 입력한다.
2. 문서의 `/fields`를 조회하고 기존 PATCH 경로로 값을 확인한다. 조건은 `/conditions`에 저장한다.
3. `/recompute-status`의 `activeJob`, `latestJob`과 `results`를 polling한다. `PENDING/RUNNING` 뒤 `SUCCEEDED/PARTIAL/FAILED/SUPERSEDED`를 처리한다. PARTIAL은 확인되지 않은 계산 범위가 남았다는 뜻이다.
4. `/results`의 네 섹션·항목 상태·freshness를 표시한다. `null`은 미확인으로 표시하고 0원으로 치환하지 않는다.
5. 최신 결과에서 `/cases/{id}/reports`를 명시적으로 요청한다. `PROCESSING` → `READY/FAILED`를 polling하고 Bearer 헤더로 PDF/ZIP을 내려받는다.

새 비교 묶음 API·검토 작업실도 유지된다. 새 묶음은 기존처럼 문서 선택→confirmed-input→analyze로 실행한다. 기존 Case 입력은 내부 호환 묶음에서 변환되며 프론트가 그 내부 ID를 관리할 필요가 없다.

## 호환 정책

- 채팅 상세는 기존 클라이언트를 위해 기본 `messages` 배열을 유지한다. `GET .../chats/{id}?page=1&size=20`은 `{session,messages:{items,page,size,total}}`를 반환한다. 세션의 messageCount/lastMessagePreview/updatedAt도 제공한다.
- 채팅 취소 상태는 기존 명세와 같은 `CANCELLED`, 삭제 성공은 기존 `204`를 유지한다.
- 수기 기본급 산술은 사용자가 산정 범위·시간을 확인한 경우에만 agent 규칙에서 수행한다. 법정수당을 임의 추정하거나 소계를 grossPay로 확정하지 않는다.
- 기존 Case 지급 내역 비교는 은행+현금 배분을 표시·대조하면서 USER_REPORTED/USER_CONFIRMED를 보존한다. 이는 실제 수령 사실의 인증이 아니다. 새 비교 묶음의 확인된 은행 거래 중심 정책은 유지한다.
- 소수 단위가 있는 새 agent 결과를 기존 정수 KRW 뷰로 표현할 수 없으면 잘라서 표시하지 않고 REVIEW_REQUIRED/null로 보낸다. 원시 agent 결과의 정밀도는 보존한다.
- 보고서는 요청 시점의 입력·결과 참조·선택 원본을 고정한다. 이후 변경하면 isCurrent=false이며 기존 PDF는 바뀌지 않는다. 원본 삭제·연결 철회 후에는 과거 산출물 접근도 차단한다.
- ZIP은 기존 agent 파일에 `result-snapshot.json` 이름도 추가했다. PDF와 ZIP 안 `summary.pdf`는 동일하다.

## 검증과 한계

실제 runner·규칙·SQLite·권한·파일·PDF 생성 코드를 실행했다. 추출/자유 채팅 성공 케이스의 외부 모델 응답만 명시적인 테스트 대역을 사용했다. 실제 Docker TCP에서도 수기 입력→자동 검사→현금 배분→독립 버전→회사 문의→기존 PDF/ZIP과 새 비교 묶음·검토 작업실을 확인했다.

- 전체 회귀: **752 passed / 9 skipped**, 13 warnings, 32.96초. 선택 코퍼스 8개·Windows 전용 1개는 제외. `docs/contract-audit/fixed-pytest.txt`.
- 첨부 **92개 경로 등록 / 0개 누락, 38개 비교 시나리오 일치 / 0개 차이**: `docs/contract-audit/fixed-findings.json`.
- 실제 Docker HTTP: `fixed-http.json`, `fixed-modern-http.json`.
- OpenAPI: **102 path / 138 HTTP 작업**, 빈 JSON 성공 스키마 0. `docs/openapi.json`. 핵심 결과·필드·Case DTO는 구체 타입이며 동적 agent/검토 응답 일부는 JsonValue envelope다. 모든 DTO가 완전히 세분화된 SDK 스키마라는 뜻은 아니다.

프론트가 로컬에서 실행되지 않은 것은 오류로 계산하지 않았다. 실제 프론트 브라우저·EC2 배포·유료 모델의 응답 품질은 이 로컬 검증 범위에 포함하지 않는다. 이전 실패 근거는 `FRONTEND_CONTRACT_AUDIT.md`와 `findings.json`에 당시 기록으로 보존한다.
