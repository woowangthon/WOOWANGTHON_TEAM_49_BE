"""Export inspectable local evaluation results without making model calls."""

import csv
import io

from agent.tests.support.contract_corpus import DEFAULT_OUTPUT, read_json, write_json
from agent.tests.support.evaluate_corpus import summarize


def export(output=DEFAULT_OUTPUT):
    report = summarize(output)
    lines = [
        "# 계약서 PDF 검증 결과",
        "",
        "검증 PDF 37건 · 모델 gpt-6-luna · 운영 출시 준비 완료를 의미하지 않음",
        "",
        f"- 추출 완료 {report['completed']}/37건, 검증 거절 {report['failed']}건",
        f"- 명시값 추출 {report['positive_values']['correct']}/{report['positive_values']['total']}항목 (실패 문서의 양성 항목도 분모에 포함)",
        f"- 물리 단위·기간 일치 {report['unit_period']['correct']}/{report['unit_period']['total']}항목",
        f"- 완료 문서의 빈칸 오인식 {report['blanks']['hallucinated']}/{report['blanks']['total']}항목",
        f"- 판단 보류 {report['unscored']}항목: 정확도 분모에서 제외",
        f"- 인용 페이지 유효 {report['evidence']['valid_pages']}/{report['evidence']['predicted_values']}건; 텍스트 대조 {report['evidence']['text_verified']}건, 이미지 사용자 확인 필요 {report['evidence']['vision_requires_confirmation']}건",
        "",
        "문자열 값만 일치해도 통과시키지 않습니다. 금액·분 단위는 물리 단위를 함께 비교합니다. 날짜·시각·임금 형태는 고정 필드의 타입과 값으로 비교합니다. 이미지 근거는 페이지 범위만 코드로 검증되며, 인용문 자체의 독립 OCR 정확도는 측정하지 않았습니다.",
        "",
        "## 37건별 결과",
        "",
        "| 자료 | 종류 | 실행 | 명시값 | 빈칸 오인식 | 결과 |",
        "|---|---|---|---:|---:|---|",
    ]
    table, detail = [], []
    for record in report["records"]:
        positive = [f for f in record["fields"] if f["gold_state"] == "present"]
        blank = [f for f in record["fields"] if f["blank_hallucination"] is not None]
        matched = sum(f["exact"] for f in positive)
        hallucinated = sum(f["blank_hallucination"] for f in blank)
        doc_id = record["document_id"]
        title = record["title"].replace("|", "/")
        lines.append(
            f"| [{doc_id}](pdfs/{doc_id}.pdf) {title} | {record['kind']} | {record['status']} | {matched}/{len(positive)} | {hallucinated}/{len(blank)} | [JSON](results/{doc_id}.json) |"
        )
        table.append(
            {
                "document_id": doc_id,
                "kind": record["kind"],
                "status": record["status"],
                "positive_correct": matched,
                "positive_total": len(positive),
                "blank_hallucinated": hallucinated,
                "blank_evaluated": len(blank),
                "error_code": record["error"]["code"] if record["error"] else "",
            }
        )
        detail.extend({"document_id": doc_id, **f} for f in record["fields"])
    lines.extend(
        [
            "",
            "## 실패와 누락",
            "",
            "json-03, json-04, json-07은 두 번의 형식·근거 검증 시도 후 MODEL_OUTPUT_INVALID로 종료됐습니다. 원문 인용·페이지·정규화 검증 중 하나 이상을 통과하지 못했습니다. 당시 세부 검증 사유와 원시 응답은 저장하지 않아 세부 원인을 단정하지 않습니다. 실패를 빈칸 정답으로 처리하지 않았으며 다른 모델로 바꾸지 않았습니다.",
            "",
            "완료 문서에서도 json-10, json-12의 일부 근무 시각·명시된 시간이 누락되었습니다. 모든 누락은 field-results.csv와 화면의 정답 비교에서 확인할 수 있습니다.",
            "",
            "json-17의 특수점 U+2024가 맑은 고딕에서 누락되는 것을 변환 테스트가 발견했습니다. 가운데점 U+00B7로 바꾸고 매핑에 치환을 기록했습니다. 최초 평가 PDF·정답·결과·사용량은 history/에 보존했습니다. 교정본만 다시 호출했으며 이 문서의 누적 추출 호출은 총 2회입니다.",
            "",
            "## 가상 작성본 흐름",
            "",
            "가상 보조자료는 이 검증 스크립트가 명시적으로 선택했습니다. 일반 화면에서는 체크박스를 선택해야 연결됩니다.",
            "",
            "| 작성본 | 검증 결과 | 설명 | 산출물 |",
            "|---|---|---|---|",
        ]
    )
    for name, expected in (
        ("filled-hourly", "R-01 단가 차이 -1,000원/시간"),
        ("filled-monthly", "R-06 입금 차이 100,000원"),
        ("filled-missing", "R-01 정보 부족"),
    ):
        flow = read_json(output / "flows" / (name + ".json"))
        lines.append(
            f"| {name} | {expected} | {flow['explanation']['status']} | [PDF](reports/{name}.pdf) · [ZIP](reports/{name}.zip) |"
        )
    usage = report["usage"]
    lines.extend(
        [
            "",
            "## 실제 호출 사용량",
            "",
            f"- 호출 예약 횟수: {report['model_calls']}회 (37건 평가·교정 재평가·가상 설명 3건·브라우저 추가 검증 합계)",
            f"- 확인된 입력 토큰: {usage.get('input_tokens', 0):,}",
            f"- 확인된 출력 토큰: {usage.get('output_tokens', 0):,}",
            f"- 확인된 전체 토큰: {usage.get('total_tokens', 0):,}",
            f"- 사용량 미확정 이력이 있는 작업: {report['usage_incomplete_jobs']}건",
            "- 업로드 추출 취소 후 같은 작업을 이어서 실행했습니다. 이 작업의 누적 예산은 3회이며, 취소된 1회의 청구 여부·토큰 수는 미확정입니다. 반환된 응답의 토큰만 합산했습니다.",
            "- 금액 비용은 실시간 요율·청구 내역을 확인하지 않아 환산하지 않았습니다.",
            "",
            "## 재현과 한계",
            "",
            "본문 재구성은 원본 지면 복원이 아닙니다. 표는 셀 순서를 유지한 텍스트로 펼쳤습니다. HWP 14건은 내장 PNG의 순서·비율·복원 픽셀 해시가 일치합니다. 작성본은 원형 양식 하단에 한국어·영어 부속사항을 작성해 전체 페이지를 이미지로 만들었습니다.",
            "",
            "정답은 모델 호출 전 원문에서 작성했습니다. 조항 라벨은 위치를 찾는 데만 사용했습니다. 원문·문단·페이지 매핑은 mappings/, 정답은 gold/, 호출 전 입력 해시는 usage/*.inputs.json에 있습니다.",
            "",
            "작은 로컬 표본이며 빈 양식 비중이 큽니다. 이 수치는 실제 서비스의 전체 정확도가 아닙니다. W-01~W-05와 R-07은 검증 대기 상태이고 전체 법정 임금을 계산하지 않습니다. 영속성·동시성·접근통제의 운영 통합 검증은 별도입니다.",
        ]
    )
    (output / "VALIDATION_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name, rows in (("document-results.csv", table), ("field-results.csv", detail)):
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        (output / name).write_text(buffer.getvalue(), encoding="utf-8-sig")
    write_json(output / "delivery-summary.json", {k: v for k, v in report.items() if k != "records"})
    return report


if __name__ == "__main__":
    result = export()
    print("Exported 37 document rows, 370 field rows, known tokens:", result["usage"]["total_tokens"])
