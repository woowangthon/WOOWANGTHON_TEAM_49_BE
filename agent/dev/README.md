# 로컬 문서 작업실

## 실행과 페이지

프로젝트 루트에서 실행합니다. Python 3.14 가상환경과 [로컬 읽기 준비](OCR.md)를 확인하고, AI 처리의 키·모델은 `agent/.env`에 설정합니다.

```powershell
.\.venv\Scripts\python.exe -m agent.dev.test_server --port 4190
```

| 페이지 | 기능 | 모델 호출 |
|---|---|---|
| [원문 읽기](http://127.0.0.1:4190/ocr) | 디지털 텍스트층 우선, 스캔·품질 부족 페이지는 로컬 OCR로 읽고 저장 | 없음 |
| [AI 처리](http://127.0.0.1:4190/ai) | 저장 텍스트로 기본 후보 A 분류, 선택한 확인 필요 항목은 원본 PDF로 B 보완 | A·B를 각각 명시적으로 실행할 때 발생 |
| [계산·대조](http://127.0.0.1:4190/compare) | 자료 연결·기간·근로조건 확인 | 없음 |
| [결과](http://127.0.0.1:4190/results) | 임금 계산·대조·근거·과거 결과 | 없음 |
| [확인 질문](http://127.0.0.1:4190/questions) | 부족한 조건·날짜·답변과 재계산 | 없음 |
| [회사 문의](http://127.0.0.1:4190/inquiry) | 초안 생성·편집·저장·복사 | 없음 |
| [자료 받기](http://127.0.0.1:4190/reports) | 원본별 선택·PDF·ZIP | 없음 |
| [쉬운 용어](http://127.0.0.1:4190/glossary) | 공통 사전 검색·쉬운 예시 | 없음 |

업로드와 페이지 이동은 AI를 호출하지 않습니다. A는 저장한 텍스트만 전송합니다. B는 별도 실행 시 **원본 전체 PDF를 OpenAI에 보내며 추가 비용이 발생**합니다. 판독 대상은 선택 항목으로 제한하지만 전송 파일을 그 항목·페이지만으로 잘라 보내지는 않습니다.

## 사용 순서

1. **원문 읽기**에서 PDF를 업로드하고 읽기를 실행합니다. 페이지별로 디지털 텍스트층의 품질을 검사한 뒤 사용하거나 OCR로 전환합니다. 원본 미리보기·읽기 방식·저장 텍스트를 확인합니다.
2. **AI 처리**에서 저장 원문을 확인하고 **AI 처리 실행**을 눌러 기본 후보 A를 만듭니다. 기본값 `auto`는 계약서·근무기록·급여명세서·입금기록을 분류합니다.
3. 필요한 경우 A의 확인 필요·미기재 항목 중 아직 확정하지 않은 1~50개를 선택해 **PDF 보완 읽기 B**를 실행합니다. A/B의 값·단위·페이지·인용을 원문과 확인한 뒤 검사 통과 후보를 선택하거나 직접 입력하고, 어려운 항목은 보류합니다. B 완료나 A/B 일치는 자동 확정이 아닙니다.
4. **문서 대조**에서 새 비교 묶음을 만들고 자료를 선택합니다. 계약서·명세서는 한 건씩, 근무기록·입금기록은 여러 건을 연결할 수 있습니다. 문서별 근로자·사업장·기간·임금 단위와 검토 필요 항목 수를 확인합니다.
5. 묶음의 급여 기간과 대상자를 확인하고 양쪽 값을 비교합니다. 다른 대상의 자료는 원문을 확인해 보완하거나 제외해야 하며 확인 표시로 충돌을 건너뛸 수 없습니다. 시간은 각각 실근로·유급 기준을 확인하고 소계·총계와 세부 항목을 구분합니다. 항목 완전성·입금 범위는 실제로 확인한 경우에만 선택합니다.
6. **확인한 값으로 비교**를 누르면 입력 확인 후 대조를 연속 실행합니다. 결과에서 값·차이·근거를 확인합니다. 설명 생성과 보고서는 별도 버튼입니다.

기본 읽기 결과와 A가 없으면 B를 실행할 수 없습니다. B는 A 전체 실패나 OCR 실패를 자동 우회하는 기능이 아닙니다. AI 처리된 자료가 없으면 대조 입력 확인도 막습니다. 계약서가 없어도 명세서·입금 등 필요한 자료가 확인된 비교는 독립 실행할 수 있습니다.

같은 문서에서 로컬 읽기·A·B를 동시에 실행하지 않습니다. 취소·중단 작업은 입력·설정·원본·A 버전이 같으면 누적 호출 예산을 이어받아 실행합니다. 이미 시작된 A/B 요청은 취소해도 사용량이 발생할 수 있습니다.

로컬 읽기·A 재추출·유형 변경으로 기준이 바뀌면 이전 B·선택은 이력에 남기고 현재 적용에서 제외합니다. 수정 초안은 기존 재추출 보존 규칙을 따르며, 수동 유형 변경은 기존 확인 입력을 초기화합니다. 기존 자료를 자동 재실행하거나 B로 자동 전송하지 않습니다.

## 저장과 원문 확인

`output/document-workspace/`에 `documents.json`, `uploads/`, `cases/`, `comparisons/`, `jobs/`, `artifacts-and-checkpoints/`를 저장합니다. 문서 목록은 직접 업로드한 파일만 표시하며 비교 묶음은 별도 목록과 `?comparison=...` 주소로 복원합니다.

| 저장 항목 | 내용 |
|---|---|
| `case.ocr.pages` | 페이지별 로컬 읽기 텍스트. 호환용 `ocr` 이름 유지 |
| `case.ocr.ocr_pages` | 행과 OCR인 경우의 신뢰도. 디지털 텍스트에 가짜 OCR 신뢰도를 붙이지 않음 |
| `case.ocr.page_methods` / `source_method` | 페이지별 `pdf_text / python_ocr`, 전체 `pdf_text / python_ocr / mixed` |
| `case.extraction_a` / `extraction_a_revision` / `extraction_a_context` | 기본 A 원본 후보·버전·원본 해시/읽기 결과/실행 연결 |
| `case.supplements` | A 버전과 연결된 B 실행별 후보. B는 `source_method=pdf_supplement` |
| `case.extraction.fields` / `evidence_catalog` | 현재 적용 후보와 페이지·인용 근거. 원시값·제안값·검사 통과값·상태·사유를 분리 |
| `case.candidate_selections` / `candidate_selection_history` | 사용자 선택·직접 입력과 원본 해시·작업·실행·근거 이력 |
| `comparison.materials` | 선택 문서·추출 버전과 항목별 근거 |
| `comparison.draft` / `confirmed` | 이번 묶음의 수정·확인값과 대상 정보 |
| `comparison.analysis_history` | 실행 당시 기간·입력·자료를 고정한 결과 이력 |

문서별 읽기·AI 화면의 선택과 비교 묶음 선택은 독립적입니다. 비교용 수기 수정은 묶음에만 반영됩니다. 문서 후보 선택·원본 재추출·유형 변경은 참조 묶음을 갱신 필요로 표시합니다. B 완료만으로 현재 `extraction`이나 비교 입력을 교체하지 않습니다. 후보 선택 후 관련 묶음의 자료를 다시 불러와 확인해야 합니다.

근거는 디지털 텍스트층 `text_match`, OCR `ocr_unverified`, B 판독 `vision_unverified`로 구분합니다. `text_match`도 저장 텍스트와의 일치일 뿐 원본 화면의 정확성이나 사용자 확인 완료를 뜻하지 않습니다. A/B 일치로 확인을 생략하지 않습니다.

검사 상태는 `valid / needs_review / missing`입니다. 보류 후보의 `normalized_value`는 null이며 잘못된 근거의 참조는 비웁니다. AI가 주장한 인용·페이지는 검토용으로 따로 남깁니다. `valid`도 사용자 확인이 필요합니다. 항목명·날짜 등 필수 정보가 없는 행은 금액만 입력해 정상 처리할 수 없습니다. 명세서 문제 행은 제외 후 수기로 보완할 수 있고, 확인되지 않은 입금 거래는 선택할 수 없습니다. B 완료나 새 후보의 존재만으로 항목 완전성·입금 범위를 확정하지 않습니다.

PDF 페이지 미리보기에는 Poppler의 `pdftoppm`이 필요합니다. PATH에 없으면 `AGENT_PDFTOPPM_PATH`를 지정합니다. 보고서 글꼴은 `AGENT_FONT_PATH` 또는 Windows 맑은 고딕을 사용합니다.

## 로컬 API

변경 요청은 `X-Local-Agent: 1` 헤더와 JSON 본문을 사용합니다. PDF 업로드는 `application/pdf` 본문입니다.

| 메서드 · 경로 | 기능 |
|---|---|
| `GET /api/health` | 엔진·모델·단계별 기능 상태 |
| `GET /api/documents` | 실제 업로드 문서 목록 |
| `POST /api/uploads?document_type=auto` | PDF 업로드·문서 사례 생성 |
| `GET /api/documents/{id}/pdf` | 원본 PDF |
| `GET /api/documents/{id}/pages/{page}.png` | 원문 페이지 미리보기 |
| `GET /api/cases/{id}` | 저장 원문·A/B 결과·후보 선택·초안 |
| `PUT /api/cases/{id}/document-type` | AI 분류 방식 변경 |
| `POST /api/cases/{id}/jobs` | `ocr`(로컬 읽기) / `extract`(A) / `supplement`(B) / `analyze` / `explain` / `report` |
| `POST /api/cases/{id}/candidate-selection` | 원문 확인 후 A/B 후보 또는 직접 입력 선택 |
| `GET /api/jobs/{id}` | 상태·실패·누적 사용량 |
| `POST /api/jobs/{id}/cancel` · `/resume` | 취소·재개 |
| `GET /api/comparison-sources` | 처리 상태·대상·기간·단위·검토 수·요약 후보 |
| `GET` · `POST /api/comparisons` | 비교 목록·생성 (`title`, `period_start`, `period_end`, `selection`) |
| `GET /api/comparisons/{id}` | 독립 묶음과 조합 적합성 안내 |
| `PUT /api/comparisons/{id}/materials` · `/draft` | 자료 연결·비교용 수정 저장 |
| `POST /api/comparisons/{id}/confirm` · `/jobs` | 대상·입력 확인 및 `analyze` / `explain` / `report` |
| `PUT /api/cases/{id}/materials` · `/draft` | 기존 문서 사례의 자료 연결·초안 저장 |
| `POST /api/cases/{id}/confirm` | 기존 문서 사례의 확인 입력 확정 |
| `GET /api/artifacts/{id}` | 결과 버전에 연결된 보존 PDF·ZIP |

`ocr`는 디지털 텍스트 우선·OCR 대체를 포함한 호환 단계명입니다. `extract`는 저장 텍스트에 대한 A입니다. API가 읽기 결과 ID를 고정하므로 임의 원문 문자열을 직접 받지 않습니다. 작업 생성은 HTTP 202와 작업 ID를 반환합니다.

B 생성 본문은 `{ "stage": "supplement", "field_ids": ["net"], "extraction_revision": 현재버전 }`입니다. A의 `needs_review / missing`이며 현재 미확정인 항목을 중복 없이 1~50개만 허용합니다. 후보 선택 본문에는 `field_id`, `candidate_id`, `source_confirmed: true`, `extraction_revision`이 필요합니다. `candidate_id`는 `A`, `보완작업ID:필드ID`, 직접 입력의 `manual`이며 직접 입력에는 `value`·`unit`을 함께 보냅니다. 형식·원문 확인·버전 검사를 통과해야 선택할 수 있습니다. 보류는 새 값을 적용하지 않으며 기존 확인값을 삭제하지 않습니다.

공개 추출 계약 기본값은 `extraction_version=1.2`이며 `extraction_method`·`target_fields`·`source_method`·`page_methods`를 추가했습니다. 계산 엔진 `agent-0.4.0`과 추출 호환 `extraction-1.2`를 분리하고 공개 스키마·체크포인트 `1.0` 및 과거 결과를 호환해 읽습니다.

새 비교 묶음은 `kind=comparison`, `document_id=null`이며 로컬 읽기·A·B 실행 대상이 아닙니다. `subjects_confirmed`는 동일 대상 확인, `source_identity`는 비교용 대상 정보 보완, `worked_time_kind`·`slip_time_kind`는 각각의 시간 의미입니다. `slip_actual_minutes`와 `slip_paid_minutes`는 따로 보존하며 비교 입력의 `slip_minutes`에는 사용자가 비교할 시간을 확인합니다. 기존 `time_kind`와 문서 사례 API는 호환을 유지합니다.

`compatibility.issues`는 `blocking / review / info`로 구분합니다. 대상 불일치는 확인을 차단하며 단위·기간·시간 의미 문제는 관련 항목의 검토 사유입니다. 가능한 다른 대조는 유지합니다. 입금일을 급여기간과 자동 동일시하지 않고 사용자가 거래·배분액·범위를 확인합니다.

## 현재 한계

단일 사용자·단일 프로세스용 개발 도구입니다. 운영 로그인·자료 격리·DB·작업 큐·운영 삭제 보장은 별도 범위입니다. 표준 1일8시간·주40시간의 시급·월급 W-01~05와 R-07을 지원합니다. 특수 근무·단시간·월중 입퇴사 안분·계약 변경 등은 관련 항목을 보류합니다. 금액 차이를 미지급 확정으로 표현하지 않습니다. [입력 계약·지원 범위](../PAYROLL.md)를 참고하세요. 문서 인식 정확도와 실제 모델 비용 평가는 테스트용 공급자 검증과 구분합니다.
