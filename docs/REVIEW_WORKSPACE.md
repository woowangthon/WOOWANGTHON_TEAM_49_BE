# 1차 문서 검토 통합

기준: `feat/devmerge-integration`의 `e75b10b`와 `feat/document-review-workspace`의 `9f3669b`. 변경·이식 대상은 `devmerge/`뿐이다. 루트 agent, 별도 개발 HTTP 서버, JSON LocalStore와 개발 화면은 통합 실행 경로에 사용하지 않는다. 공개 계약 1.1/기존 1.0 호환, 사례 없는 기존 추출, 원본/파생 PDF 구분과 원본 ZIP 출처 기록을 유지한다.

## 역할

| 경로 | 역할 |
|---|---|
| `agent/review/pipeline.py`, `model.py`, `registry.py` | 내부 Python 문서 그래프, 분류·추출·제한된 이미지 검증, 필드·도구 정책 |
| `agent/review/reading.py`, `rows.py`, `state.py` | 텍스트/OCR 좌표, 실제 원문 행 연결, 편집·충돌·확정·재처리 병합 규칙 |
| `agent/review/telemetry.py`, `flow.py` | 실제 호출 사용량과 관측한 분기 기록. UI용 가상 실행 기록을 만들지 않음 |
| `app/review_store.py` | 사용자별 SQLite 초안·자료·작업·수정 이력·불변 연결 스냅샷 |
| `app/review_service.py` | 권한·버전·파일 검증, 큐 등록·실행·취소·복구와 게시 |
| `app/review_bridge.py` | 확인값과 원문을 기존 documents/후보/확정/비교 묶음/작업 구조로 연결 |
| `app/routers/reviews.py` | 기존 로그인 인증을 사용하는 `/api/v1/reviews` API |

## API

모든 경로는 `/api/v1` 아래에 있다. JSON 요청은 snake_case를 사용하며 기존 응답 래퍼 `{code,message,data}`를 유지한다. 변경 요청의 `revision`은 최신 GET 응답 값을 전달한다. 충돌 시 409이며 자동 덮어쓰기를 하지 않는다. 상세 요청 스키마는 `/docs`와 `docs/openapi.json`에서 확인한다.

| 메서드·경로 | 동작 |
|---|---|
| `POST /reviews` | `{title?,auto_process?,auto_verify?}` 초안 생성. 자동 처리는 기본 false |
| `GET /reviews?page=1&size=20` | 사용자 초안 목록과 문서별 필드 프로필 |
| `GET /reviews/{id}` | 기본 정보·자료·필드·행 묶음·작업·부족 요건·연결 이력 |
| `PUT /reviews/{id}` | `{revision,basic?,options?,confirm_basic?,auto_process?,auto_verify?}` |
| `DELETE /reviews/{id}?revision=N` | 초안 접근 차단·진행 작업 취소. 이미 연결한 사례는 별도 삭제 |
| `POST /reviews/{id}/documents` | multipart `revision`, 반복 `files` 원본 등록 |
| `POST /reviews/{id}/documents/{doc}` | `{revision,action,parts?,field_ids?}` 처리·분류·수동·재검증·제외·포함 |
| `GET /reviews/{id}/documents/{doc}/original` | 실제 원본 MIME과 바이트. 권한·해시 검사 |
| `GET /reviews/{id}/documents/{doc}/pages/{n}.png` | 실제 원문 페이지. 1부터 시작 |
| `PUT /reviews/{id}/fields` | `{revision,field_id,value?,unit?,candidate_id?,defer?}` |
| `POST /reviews/{id}/fields` | `{revision,part_id,row_type?,label?}` 반복 행 또는 추가 정보 생성 |
| `POST /reviews/{id}/confirm` | `{revision,field_ids,source_seen,conflict_acknowledgement?}` |
| `GET /reviews/{id}/jobs/{job}` | 상태·실제 모델/도구 호출·토큰 사용량 |
| `POST /reviews/{id}/jobs/{job}` | `{revision,action:"cancel"|"resume"}` |
| `GET /reviews/{id}/history?page=1` | 수정·확정·처리·연결 이력, 페이지당 50개 |
| `POST /reviews/{id}/complete` | `{revision,workplace_id?,case_id?,analyze?,calculation_input?,selected_transaction_ids?}` |

`basic`은 `employer_name,pay_type,period_start,period_end,payment_date`다. pay_type은 `HOURLY` 또는 `MONTHLY`, 날짜는 ISO 날짜다. 기본 정보 수정은 기본 정보 확정을 해제한다. `confirm_basic=true`는 현재 기본 정보를 명시적으로 확인한 동작이다.

`options`는 `subjects_confirmed,earnings_complete,deductions_complete,payment_scope_confirmed,worked_time_kind,slip_time_kind,contract_part_id,payslip_part_id`다. 같은 검토에 계약서/명세서가 여러 개면 기준 part ID를 선택한다. 시간 의미는 `actual` 또는 `paid`이며 지정하지 않았다고 추정하지 않는다.

자료 action은 `process,classify,manual,verify,exclude,include`다. `parts`의 각 항목은 `{document_type,start_page,end_page,label?}`이며 전체 페이지를 중복·누락 없이 덮어야 한다. 유형은 `contract,payslip,attendance,deposit`이다. `manual`은 모델 없이 필드·행을 입력하는 경로이며 스캔 판독 실패 시에도 사용한다. `classify`는 사용자가 지정한 구간으로 추출을 실행한다. `verify`는 현재 미확정 필드 ID 1~50개와 기존 판독 결과를 요구한다.

## 확인 경계

- 추출값, 사용자가 수정한 값, 사용자가 확정한 값은 별도 상태다. 기본 프로필의 빈 필드는 임의로 0이나 일반 조건으로 채우지 않는다.
- 실제 상충 후보가 있으면 `conflict_id`를 반환한다. 기준 후보 선택 또는 직접 수정 후 `source_seen=true`와 현재 `conflict_acknowledgement`로 그 항목 하나만 확정해야 한다. 일괄 확정으로 우회할 수 없다.
- 표의 같은 행임을 원문으로 입증한 셀만 함께 표시하도록 `rows`를 반환한다. 각 칸은 개별 필드다. 행 묶음은 값의 정확성이나 확정을 뜻하지 않는다.
- 필드 `evidence.boxes`는 정규화된 `[x0,y0,x1,y1]` 목록이다. 해당 페이지 PNG와 같은 방향을 사용한다. 정확한 위치를 찾지 못하면 빈 목록과 `location_status=unavailable`을 반환한다. 수기 값을 원문에서 추출한 값으로 표시하지 않는다.
- 처리 중 사용자가 필드를 수정하면 그 작업은 STALE로 남고 기존 수정값을 덮어쓰지 않는다. 재처리 후에도 수정·삭제·보류값을 보존하고 다시 확정을 요구한다. 반복 행은 원문 인용 구간이 일치할 때만 이전 값과 대응한다.

## 계산 연결

`complete`는 기본 정보·동일 대상·충돌 해결·사용할 확인값을 검사한다. 명시한 기존 사업장/사례는 소유권·이름·급여기간·시급/월급이 일치해야 한다. 기존 사례를 명시하지 않으면 실제 새 사례를 만든다. 가짜 case ID를 생성하지 않는다.

원본과 확인 이력을 기존 `documents`, `document_ocr`, `extraction_candidates`, `document_confirmations`에 저장하고 독립 비교 묶음을 생성한다. 원본 PDF의 분리 구간은 별도 자료로 연결하되 전체 원본 페이지 번호와 원본 바이트를 보존한다. 같은 원본의 겹치는 구간은 기존 비교 묶음 API와 입력 스냅샷 생성에서도 거절한다. 연결된 구간 문서의 재추출은 작업실에서 재처리한 후 새로 연결해야 한다.

`calculation_input`은 사용자가 확인한 `wage_conditions,work_records,coverage,context_requirements`를 받는다. 나머지 문서 금액·항목은 서버가 확정 필드에서 생성하므로 클라이언트가 미확정 값을 끼워 넣을 수 없다. 여러 근태 자료의 총 시간을 임의로 더하지 않으며 부족한 계산 조건은 기존 결과의 부족 상태로 남는다.

입금 원장의 등록·배분은 기존 API를 사용한다. 완료 후 연결 자료의 deposit 후보 ID와 `transaction_N` 원문 행을 이용해 DOCUMENT 거래를 등록하고 사례에 배분할 수 있다. 비교 묶음의 `confirmed-input`에서 그 거래 ID와 `payment_scope_confirmed`를 명시하고 analyze를 요청한다. 같은 원금을 중복 소비하는 새 배분 구현을 추가하지 않았다.

`analyze=true`가 기본이며 동일 revision과 동일 완료 요청은 같은 연결 ID/작업 ID를 반환한다. 같은 revision에 다른 요청을 보내면 409다. 연결 후 초안을 수정하면 작업실의 이전 연결 이력에 `stale=true`가 표시된다. 이미 생성한 비교 묶음은 당시 확인값의 독립 스냅샷이며 현재 초안으로 덮어쓰지 않는다. 이후 비교 묶음 입력 변경은 기존 버전·최신성 규칙을 따른다.

## 실행과 검증 범위

기존 작업과 공통 semaphore를 사용하고 검토별 문서 처리를 직렬화한다. HTTP 요청은 작업을 큐에 넣고 반환한다. DB 연결을 모델 호출이나 OCR 동안 열어두지 않는다. 중단 후 자동 유료 재호출은 하지 않으며 사용자 재개 시 저장된 성공 응답을 재사용한다. 같은 단계·입력의 실패 시도 횟수는 재개해도 유지하고 AgentSettings의 `max_stage_calls` 한도를 적용한다.

자동 검증은 합성 문서·가짜 모델을 사용한다. 실제 디지털 PDF 판독·좌표·페이지 PNG·SQLite·확인·계산 엔진·PDF/ZIP은 실제 경로로 검증한다. 실제 유료 모델 정확도, 실제 스캔 OCR 품질, Next.js 화면, 다중 프로세스 운영을 검증한 것으로 해석하지 않는다.

AI 추천·추천값 임시 적용·검토 안내 채팅은 후속 범위다. 현재 기존 자유 채팅 API는 유지한다.
