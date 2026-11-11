# 표준 임금 계산과 개발 화면

선택한 같은 근로자·사업장·급여기간의 자료와 사용자 확인값으로 계산합니다. 등록한 모든 문서를 합치지 않습니다. 급여명세서·입금기록만 선택하면 기존 대조를 그대로 사용할 수 있습니다.

## 지원 범위

- H-STD 시급제와 M-STD 월급제의 기본급·주휴·연장·야간·휴일 W-01~05, 계산 임금과 같은 범위의 급여명세서 R-07.
- 최초 범위는 성인, 1일 8시간·주 40시간의 고정 근무, 기간 중 같은 계약입니다. 근무 요일과 주 시작일은 확인 입력입니다. 적용기간은 2025-01-01~2026-12-31입니다.
- 단시간·특수 근무제, 계약 변경, 월중 입퇴사·결근에 따른 월급 안분, 자정에 휴일 성격이 바뀌는 근무 등은 영향을 받는 항목만 확인 필요로 남깁니다.
- 월 기준시간·통상임금·포함 수당은 확인값을 사용합니다. 209시간을 자동 입력하지 않습니다. 주휴 포함 금액은 표시하되 총액에 다시 더하지 않습니다. 금액은 중간 반올림 없이 각 항목의 최종 원 단위에서 HALF_UP 처리합니다.
- 미확인·모름·확인 예정·충돌은 확인한 0과 구분합니다. 전체 범위가 확인된 경우에만 총액을 표시합니다. 차이는 미지급 확정이 아닙니다.

공식 근거·기간·검증 사례는 [지원표](contracts/support_manifest.json), 실제 계산은 `rules/wages.py`, 독립 기대값은 `tests/test_standard_wages.py`에 있습니다. 지원표의 완료는 이 규칙 범위의 검증이며 운영 서비스나 AI 판독 품질 인증이 아닙니다.

## 공개 입력·출력

`InputSnapshot.wage_conditions`는 선택 입력입니다. 각 조건은 `{state, value, confirmation_id, evidence_refs}`이며 알려진 값만 `state=known`으로 전달합니다. 숫자는 금액 문자열·분 정수, 사업장 조건은 참/거짓, 일정·날짜·수당 구성은 별도 구조입니다.

| 입력 | 내용 |
|---|---|
| `standard_employment`, `contract_unchanged`, `five_or_more_workers` | 근무형태·계약 변경·사업장 적용 조건 |
| `full_period_employment`, `records_complete`, `composition_complete` | 전체 재직·기록 누락·임금 구성 확인 |
| `monthly_weekly_rest_included` | 월급에 주휴 포함 여부 |
| `ordinary_hourly_rate` 또는 `ordinary_monthly_amount`, `monthly_standard_minutes` | 수당의 기준 임금·월 기준 분 |
| `schedule` | 근무 요일별 분·주 시작 요일·주휴일·주휴 분 |
| `days` | 날짜별 근무일·무급휴무·유급휴일·주휴 구분과 출근 상태 |
| `included_allowances` | 이미 포함된 항목별 금액 |
| `statement_mappings` | W 항목 묶음과 명세서 행 ID·동일 기간·근거 |
| `calculation_period`, `work_records`, `coverage` | 계산 기간·시간대 포함 출퇴근/휴게·확인한 기록 범위 |

`WageItemResult`는 `amount`(산정액), `included_amount`(포함 금액), `additional_amount`(추가분), `payable_amount`(총액에 합산할 분)를 구분합니다. R-07의 `wage_items`는 항목별 대응·독립 검증 여부를 제공합니다. 질문은 대상 조건·이유·필요 자료·날짜·영향 규칙·용어 ID를 제공합니다.

신규 계산 요청은 snapshot과 AnalysisJobInput 모두 `rule_bundle_version=standard-wages-v1`, `support_manifest_version=standard-wages-manifest-v1`, `engine_version=agent-0.4.0`을 사용합니다. 예전 `pending-v1` 계약은 예전 비교 경로로 읽습니다. JSON 스키마는 `python -m agent.export_contracts`로 갱신합니다.

## 페이지·로컬 연결

`/ocr`, `/ai`, `/compare`, `/results`, `/questions`, `/inquiry`, `/reports`, `/glossary`는 독립 화면입니다. `?comparison=ID&result=결과ID`가 급여 건과 과거 결과를 고정합니다. 문서 화면은 `case=ID`를 함께 유지합니다.

- `PUT /api/comparisons/{id}/draft`: 위 입력을 수정한 뒤 기존 `confirm` 및 `analyze` 사용.
- `PUT /api/comparisons/{id}/answers`: `{result_id, answers:[{question_id,field_id,state,value}], confirm:true}`. 최신 결과의 관련 단일 조건만 답변 가능. 일정·기록·수당 연결은 입력 화면에서 확인. 변경 중인 초안은 먼저 재계산.
- `POST /api/comparisons/{id}/inquiry`: `{result_id,question_ids}`. 편집한 문의를 보존. `PUT`의 `{result_id,text}`로 저장. 원문 수정 이력은 결과별 보관.
- `POST /api/comparisons/{id}/jobs`: `{stage:"report",result_id,selected_source_ids:[]}`. 빈 배열은 원본 없음, 선택 목록은 해당 결과의 원본만 포함. 이전 `include_original` 참/거짓도 호환.
- `GET /api/glossary?query=...&category=...`: 문서 없이 공통 사전 조회.

운영 백엔드는 위 확인값·출처·결과 버전을 불변 스냅샷으로 저장하고 기존 AgentPorts의 권한 검사를 구현해야 합니다. `ReportInput.display_context`에 스냅샷 ID/해시, 사업장·기간, 확인 조건, 출처 파일명을 전달합니다. 보고서는 결과 버전과 수치가 일치해야 합니다. 인증·DB·작업 큐·기관 후보 필터링은 이번 범위 밖입니다.

## 보존과 API 사용

계산 엔진, 추출 호환, 보고서 서식을 별도로 버전 관리합니다. 새 기본 필드는 과거 스냅샷·결과의 해시를 바꾸지 않도록 처리합니다. 원본·A/B 후보·확인값·과거 결과·문의 편집·보고서 링크를 보존하며 자동 재추출하지 않습니다. 변경된 입력과 과거 버전은 화면·새 보고서에서 구분합니다.

기본 화면에서 AI는 A 추출과 선택 항목 B 보완에만 호출됩니다. 임금 계산·질문·설명·문의·사전·보고서는 코드와 검수 문구로 처리합니다. 공개 API의 명시적 `ExplanationInput.generation_mode=model`은 기존 연동 호환용이며 기본값은 `template`입니다.

검증 PDF 네 개는 `output/pdf/월급제_검증문서_202604`의 작성 정답을 격리 저장소에 재현합니다. 지급총액 3,272,727원, 공제 280,000원, 실수령·선택 급여입금 2,992,727원입니다. 이는 계산·연결 검증이며 실제 AI/OCR 재판독 품질 검증과 구분합니다.
