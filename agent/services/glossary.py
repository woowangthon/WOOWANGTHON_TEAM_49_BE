"""Reviewed Korean explanations shared by the UI and exported reports.

Examples are deliberately synthetic. Legal entries cite definitions rather than
attempting to decide whether a particular payment is legally owed.
"""

from copy import deepcopy

GLOSSARY_VERSION = "ko-v1"
REVIEWED_ON = "2026-11-09"
_ACT = {"title": "근로기준법 제2조", "url": "https://www.law.go.kr/법령/근로기준법/제2조"}
_HOLIDAY = {"title": "근로기준법 제55조", "url": "https://www.law.go.kr/법령/근로기준법/제55조"}
_EXTRA = {"title": "근로기준법 제56조", "url": "https://www.law.go.kr/법령/근로기준법/제56조"}
_DECREE = {"title": "근로기준법 시행령 제6조", "url": "https://www.law.go.kr/법령/근로기준법시행령/제6조"}
_MOEL = {"title": "고용노동부 임금명세서 안내", "url": "https://www.moel.go.kr/wageCal.do"}


def _entry(identifier, term, category, explanation, example, distinction, sources=()):
    return {
        "id": identifier,
        "term": term,
        "category": category,
        "explanation": explanation,
        "example": "예시(실제 사용자 금액 아님): " + example,
        "distinction": distinction,
        "sources": list(sources),
        "reviewed_on": REVIEWED_ON,
    }


_ENTRIES = (
    _entry(
        "gross",
        "지급총액",
        "급여",
        "급여명세서에서 공제하기 전 지급 항목을 더한 금액입니다.",
        "기본급 200만 원과 수당 10만 원이면 지급총액은 210만 원입니다.",
        "통장에 들어온 금액이나 실수령액과 다를 수 있습니다.",
        (_MOEL,),
    ),
    _entry(
        "deduction",
        "공제액",
        "급여",
        "급여에서 빼는 금액입니다. 세금이나 사회보험료처럼 항목별로 무엇을 뺐는지 확인합니다.",
        "지급총액 210만 원에서 15만 원을 빼면 공제액은 15만 원입니다.",
        "공제액이 있다는 사실만으로 공제가 적법한지 판단할 수 없습니다.",
        (_MOEL,),
    ),
    _entry(
        "net",
        "실수령액",
        "급여",
        "급여명세서의 지급총액에서 공제액을 뺀 금액입니다.",
        "지급총액 210만 원에서 공제액 15만 원을 빼면 실수령액은 195만 원입니다.",
        "명세서의 실수령액과 실제 입금액은 따로 비교합니다.",
        (_MOEL,),
    ),
    _entry(
        "ordinary_wage",
        "통상임금",
        "근로조건",
        "약속한 근로의 대가로 정기적이고 같은 기준에 따라 지급하기로 정한 임금입니다. 여러 수당을 계산하는 기준으로 쓰입니다.",
        "확인한 통상시급이 1만 원이면 해당 수당의 계산식에 그 시급을 넣습니다.",
        "기본급이라는 이름만으로 범위를 정하지 않습니다. 포함할 임금 항목과 계산 기준을 확인해야 합니다.",
        (_DECREE,),
    ),
    _entry(
        "scheduled_hours",
        "소정근로시간",
        "시간",
        "법에서 정한 범위 안에서 근로자와 회사가 일하기로 정한 시간입니다.",
        "하루 8시간, 주 5일로 정했다면 약속한 시간은 주 40시간입니다.",
        "실제로 일한 시간이나 급여 계산에 쓰는 유급시간과 같지 않을 수 있습니다.",
        (_ACT,),
    ),
    _entry(
        "worked_hours",
        "실근로시간",
        "시간",
        "근무 기록에서 실제로 일한 시간입니다. 확인된 휴게시간은 뺍니다.",
        "오전 9시부터 오후 6시까지 머물고 휴게가 1시간이면 실근로시간은 8시간입니다.",
        "일하지 않고도 임금이 지급되는 시간은 실근로시간에 더하지 않습니다.",
    ),
    _entry(
        "paid_hours",
        "유급시간",
        "시간",
        "급여 계산에서 임금을 지급하는 것으로 확인한 시간입니다. 실제 근무 외에 유급휴일 등의 시간이 포함될 수 있습니다.",
        "실근로 40시간에 유급휴일 8시간이 포함되면 유급시간은 48시간입니다.",
        "실근로시간과 유급시간을 같은 기준으로 비교하면 잘못된 차이가 생길 수 있습니다.",
        (_DECREE,),
    ),
    _entry(
        "payroll_period",
        "급여기간",
        "자료 대조",
        "이번 급여가 어느 날부터 어느 날까지의 근로에 대한 것인지 나타내는 기간입니다.",
        "4월 1일~4월 30일 급여를 5월 10일에 받을 수 있습니다.",
        "급여기간과 통장에 돈이 들어온 날짜는 다를 수 있습니다.",
    ),
    _entry(
        "payment_date",
        "지급일·입금일",
        "자료 대조",
        "지급일은 급여를 주기로 한 날이고, 입금일은 실제로 통장에 돈이 들어온 날입니다.",
        "4월 급여의 약속한 지급일이 5월 10일이고 입금일도 5월 10일일 수 있습니다.",
        "입금 날짜만으로 어느 급여기간의 돈인지 자동으로 정하지 않습니다.",
    ),
    _entry(
        "payment_allocation",
        "입금 배분액",
        "자료 대조",
        "한 입금 중 이번 급여에 해당한다고 사용자가 확인한 금액입니다.",
        "입금 200만 원 중 이번 달 급여가 150만 원이면 배분액은 150만 원입니다.",
        "입금 전체를 항상 급여로 보지 않습니다. 다른 달 급여나 비용 환급이 섞였는지 확인합니다.",
    ),
    _entry(
        "subtotal",
        "계산 소계",
        "계산 결과",
        "현재 조건으로 계산할 수 있었던 임금 항목만 더한 금액입니다.",
        "기본급만 계산되고 수당이 보류되면 소계에는 기본급만 들어갑니다.",
        "보류 항목이 있으면 소계를 전체 지급총액으로 보지 않습니다.",
    ),
    _entry(
        "weekly_rest",
        "주휴수당",
        "계산 결과",
        "주 단위의 근무 조건 등을 확인한 뒤 유급 주휴일에 대해 계산하는 임금 항목입니다.",
        "확인된 주휴시간이 8시간이고 기준시급이 1만 원이면 해당 항목 계산은 8만 원입니다.",
        "모든 경우에 자동으로 8시간을 넣지 않습니다. 월급에 이미 포함됐는지도 확인합니다.",
        (_HOLIDAY,),
    ),
    _entry(
        "overtime",
        "연장근로",
        "시간",
        "정해진 법정 근로시간을 넘어 일한 시간에 관한 항목입니다. 이 서비스는 지원하는 표준 근로조건 안에서 계산합니다.",
        "기록에 추가 근무 2시간이 있어도 휴게와 적용 조건을 확인한 뒤 계산에 사용합니다.",
        "퇴근이 늦었다는 사실만으로 모든 시간이 연장근로가 되는 것은 아닙니다.",
        (_EXTRA,),
    ),
    _entry(
        "night_work",
        "야간근로",
        "시간",
        "오후 10시부터 다음 날 오전 6시 사이에 일한 시간에 관한 항목입니다.",
        "오후 11시부터 자정까지 실제로 일했다면 이 시간대의 근무는 1시간입니다.",
        "야간시간이 연장근로시간과 겹칠 수 있습니다. 각각의 조건을 확인합니다.",
        (_EXTRA,),
    ),
    _entry(
        "holiday_work",
        "휴일근로",
        "시간",
        "휴일로 확인된 날에 일한 시간에 관한 항목입니다.",
        "일요일이 해당 근로자의 휴일로 확인된 경우 그날 근무를 휴일근로로 분류할 수 있습니다.",
        "달력의 토요일·일요일이라는 이유만으로 모두 휴일근로로 정하지 않습니다.",
        (_EXTRA,),
    ),
)


def glossary_entries(query="", category=None):
    """Return independent JSON-ready entries; callers cannot mutate the dictionary."""
    needle = str(query or "").strip().casefold()
    return [
        deepcopy(entry)
        for entry in _ENTRIES
        if (category is None or entry["category"] == category)
        and (
            not needle
            or needle
            in " ".join(
                str(entry[key]) for key in ("id", "term", "explanation", "example", "distinction")
            ).casefold()
        )
    ]


def glossary_for_ids(ids):
    wanted = set(ids)
    return [entry for entry in glossary_entries() if entry["id"] in wanted]
