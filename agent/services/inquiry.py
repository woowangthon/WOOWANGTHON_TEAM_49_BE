"""Pure, deterministic inquiry templates. Persistence belongs to the caller."""

from agent.services.facts import result_fact_lines

INQUIRY_TEMPLATE_VERSION = "inquiry-ko-v1"


def compose_inquiry(result, question_ids, *, workplace_name="", period=None):
    ids = list(dict.fromkeys(question_ids))
    questions = {q.question_id: q for q in result.questions}
    if any(identifier not in questions for identifier in ids):
        raise ValueError("Selected question does not belong to this result")
    lines = ["급여 자료 확인 요청", "", "안녕하세요. 급여 계산과 자료를 확인하고 싶어 문의드립니다."]
    if workplace_name:
        lines.append(f"사업장: {workplace_name}")
    if period is not None:
        start, end = (
            (period["start"], period["end"]) if isinstance(period, dict) else (period.start, period.end)
        )
        lines.append(f"확인할 급여기간: {start} ~ {end}")
    lines.extend(["", "제가 확인한 자료의 계산·대조 결과입니다.", *result_fact_lines(result)])
    if ids:
        lines.extend(["", "다음 내용을 확인해 주실 수 있을까요?"])
        for index, identifier in enumerate(ids, 1):
            question = questions[identifier]
            lines.append(f"{index}. {question.text}")
            if question.required_materials:
                lines.append("   확인할 자료: " + ", ".join(question.required_materials))
            for dates in question.required_periods:
                lines.append(f"   필요한 기간: {dates.start} ~ {dates.end}")
    lines.extend(
        [
            "",
            "자료의 차이를 미지급으로 단정하려는 것은 아닙니다. 적용한 기준과 관련 자료를 설명해 주시면 확인하겠습니다.",
            "감사합니다.",
        ]
    )
    return {
        "result_id": result.result_id,
        "result_version": result.result_version,
        "result_hash": result.result_hash,
        "selected_question_ids": ids,
        "template_version": INQUIRY_TEMPLATE_VERSION,
        "text": "\n".join(lines),
    }
