"""Display names of extracted fields in every supported language.

Korean names come from the extraction profiles (the single owner of field
definitions); this module only adds the other languages. Field values are
never touched here: they are original document content.
"""
import re

from agent.review.registry import ROLE_FIELDS
from agent.services.extraction_profiles import PROFILES

# en, vi, zh, km for each field code.
_FIELDS = {
    'employee_name': ('Employee name', 'Họ tên người lao động', '劳动者姓名', 'ឈ្មោះនិយោជិត'),
    'employee_id': ('Employee ID', 'Mã nhân viên', '员工编号', 'លេខសម្គាល់បុគ្គលិក'),
    'employer_name': ('Workplace name', 'Tên nơi làm việc', '工作单位名称', 'ឈ្មោះកន្លែងធ្វើការ'),
    'pay_type': ('Wage type', 'Hình thức trả lương', '工资形式', 'ប្រភេទប្រាក់ឈ្នួល'),
    'contract_rate': ('Wage amount', 'Mức lương', '工资金额', 'ចំនួនប្រាក់ឈ្នួល'),
    'contract_start': ('Contract start date', 'Ngày bắt đầu hợp đồng', '合同开始日期', 'ថ្ងៃចាប់ផ្តើមកិច្ចសន្យា'),
    'contract_end': ('Contract end date', 'Ngày kết thúc hợp đồng', '合同结束日期', 'ថ្ងៃបញ្ចប់កិច្ចសន្យា'),
    'work_start': ('Start time', 'Giờ vào làm', '上班时间', 'ម៉ោងចូលធ្វើការ'),
    'work_end': ('End time', 'Giờ tan làm', '下班时间', 'ម៉ោងចេញពីការងារ'),
    'break_minutes': ('Break time (minutes)', 'Thời gian nghỉ giải lao (phút)', '休息时间（分钟）', 'ពេលសម្រាក (នាទី)'),
    'scheduled_daily_minutes': ('Contracted daily working hours (minutes)',
                                'Giờ làm việc theo quy định mỗi ngày (phút)', '每日约定工作时间（分钟）',
                                'ម៉ោងធ្វើការកំណត់ប្រចាំថ្ងៃ (នាទី)'),
    'scheduled_weekly_minutes': ('Contracted weekly working hours (minutes)',
                                 'Giờ làm việc theo quy định mỗi tuần (phút)', '每周约定工作时间（分钟）',
                                 'ម៉ោងធ្វើការកំណត់ប្រចាំសប្តាហ៍ (នាទី)'),
    'payday': ('Payday', 'Ngày trả lương', '发薪日', 'ថ្ងៃបើកប្រាក់ខែ'),
    'period_start': ('Period start date', 'Ngày bắt đầu kỳ', '期间开始日期', 'ថ្ងៃចាប់ផ្តើមរយៈពេល'),
    'period_end': ('Period end date', 'Ngày kết thúc kỳ', '期间结束日期', 'ថ្ងៃបញ្ចប់រយៈពេល'),
    'worked_minutes': ('Total working time stated (minutes)', 'Tổng thời gian làm việc ghi trên tài liệu (phút)',
                       '记载的总工作时间（分钟）', 'ម៉ោងធ្វើការសរុបដែលបានកត់ត្រា (នាទី)'),
    'overtime_minutes': ('Overtime stated (minutes)', 'Thời gian làm thêm ghi trên tài liệu (phút)',
                         '记载的加班时间（分钟）', 'ម៉ោងបន្ថែមដែលបានកត់ត្រា (នាទី)'),
    'night_minutes': ('Night work stated (minutes)', 'Thời gian làm đêm ghi trên tài liệu (phút)',
                      '记载的夜间工作时间（分钟）', 'ម៉ោងធ្វើការពេលយប់ដែលបានកត់ត្រា (នាទី)'),
    'holiday_minutes': ('Holiday work stated (minutes)', 'Thời gian làm ngày nghỉ ghi trên tài liệu (phút)',
                        '记载的休息日工作时间（分钟）', 'ម៉ោងធ្វើការថ្ងៃឈប់សម្រាកដែលបានកត់ត្រា (នាទី)'),
    'work_days': ('Working days stated', 'Số ngày làm việc ghi trên tài liệu', '记载的工作天数',
                  'ចំនួនថ្ងៃធ្វើការដែលបានកត់ត្រា'),
    'payment_date': ('Payment date', 'Ngày chi trả', '支付日期', 'ថ្ងៃទូទាត់'),
    'slip_rate': ('Rate stated on payslip', 'Đơn giá ghi trên bảng lương', '工资单记载的单价',
                  'អត្រាដែលបានកត់ក្នុងប័ណ្ណបើកប្រាក់ខែ'),
    'slip_minutes': ('Hours stated on payslip (minutes)', 'Thời gian ghi trên bảng lương (phút)',
                     '工资单记载的时间（分钟）', 'ម៉ោងដែលបានកត់ក្នុងប័ណ្ណបើកប្រាក់ខែ (នាទី)'),
    'slip_actual_minutes': ('Actual working time on payslip (minutes)',
                            'Thời gian làm việc thực tế trên bảng lương (phút)', '工资单实际工作时间（分钟）',
                            'ម៉ោងធ្វើការជាក់ស្តែងក្នុងប័ណ្ណបើកប្រាក់ខែ (នាទី)'),
    'slip_paid_minutes': ('Paid-hours basis on payslip (minutes)',
                          'Thời gian tính lương/được trả lương trên bảng lương (phút)', '工资单计薪基础·带薪时间（分钟）',
                          'ម៉ោងមូលដ្ឋានបើកប្រាក់ និងម៉ោងមានប្រាក់ឈ្នួលក្នុងប័ណ្ណបើកប្រាក់ខែ (នាទី)'),
    'gross': ('Gross pay (KRW)', 'Tổng tiền lương (KRW)', '应发总额（韩元）', 'ប្រាក់ឈ្នួលសរុប (វ៉ុន)'),
    'deduction': ('Total deductions (KRW)', 'Tổng khấu trừ (KRW)', '扣除合计（韩元）', 'ការកាត់សរុប (វ៉ុន)'),
    'net': ('Net pay (KRW)', 'Lương thực nhận (KRW)', '实发金额（韩元）', 'ប្រាក់ទទួលបានសុទ្ធ (វ៉ុន)'),
    'account_holder': ('Account holder', 'Chủ tài khoản', '账户持有人', 'ម្ចាស់គណនី'),
    'account_number': ('Account number shown', 'Số tài khoản hiển thị', '账号显示', 'លេខគណនីដែលបង្ហាញ'),
    'bank_name': ('Bank', 'Ngân hàng', '银行', 'ធនាគារ'),
    'deposit_total': ('Total deposits stated (KRW)', 'Tổng tiền gửi vào ghi trên tài liệu (KRW)',
                      '记载的入账合计（韩元）', 'ប្រាក់បញ្ញើសរុបដែលបានកត់ត្រា (វ៉ុន)'),
    'employer_representative': ('Employer representative name', 'Họ tên người đại diện chủ sử dụng lao động',
                                '雇主代表人姓名', 'ឈ្មោះអ្នកតំណាងនិយោជក'),
    'employer_address': ('Company address', 'Địa chỉ công ty', '公司地址', 'អាសយដ្ឋានក្រុមហ៊ុន'),
    'workplace_address': ('Actual place of work', 'Nơi làm việc thực tế', '实际工作地点', 'ទីកន្លែងធ្វើការជាក់ស្តែង'),
    'employer_phone': ('Company contact number', 'Số liên lạc của công ty', '公司联系方式', 'លេខទំនាក់ទំនងក្រុមហ៊ុន'),
    'contract_period_type': ('Contract period type', 'Loại thời hạn hợp đồng', '合同期限类型', 'ប្រភេទរយៈពេលកិច្ចសន្យា'),
}

# Table rows are stored as <row>_<n>_<column>, e.g. transaction_3_date.
_ROWS = {
    'work': ('Daily work record', 'Bản ghi làm việc theo ngày', '每日工作记录', 'កំណត់ត្រាការងារប្រចាំថ្ងៃ'),
    'earning': ('Payment item', 'Khoản chi trả', '支付项目', 'ធាតុទូទាត់'),
    'deduction': ('Deduction item', 'Khoản khấu trừ', '扣除项目', 'ធាតុកាត់'),
    'transaction': ('Transaction', 'Giao dịch', '交易记录', 'ប្រតិបត្តិការ'),
}
_ITEM_COLUMNS = {
    'label': ('Item name', 'Tên khoản', '项目名称', 'ឈ្មោះធាតុ'),
    'amount': ('Amount (KRW)', 'Số tiền (KRW)', '金额（韩元）', 'ចំនួនទឹកប្រាក់ (វ៉ុន)'),
    'calculation': ('Calculation method stated', 'Cách tính ghi trên tài liệu', '记载的计算方法',
                    'វិធីគណនាដែលបានកត់ត្រា'),
}
_COLUMNS = {
    'work': {
        'date': ('Work date (as written)', 'Ngày làm việc (nguyên văn)', '工作日（原文）', 'ថ្ងៃធ្វើការ (តាមឯកសារដើម)'),
        'start': ('Start', 'Vào làm', '上班', 'ចូលធ្វើការ'),
        'end': ('End', 'Tan làm', '下班', 'ចេញពីការងារ'),
        'break_minutes': ('Break (minutes)', 'Nghỉ giải lao (phút)', '休息（分钟）', 'សម្រាក (នាទី)'),
        'break_interval': ('Break period (as written)', 'Khoảng nghỉ giải lao (nguyên văn)', '休息时段（原文）',
                           'ចន្លោះពេលសម្រាក (តាមឯកសារដើម)'),
        'worked_minutes': ('Worked (minutes)', 'Làm việc (phút)', '工作（分钟）', 'ធ្វើការ (នាទី)'),
        'overtime_minutes': ('Overtime (minutes)', 'Làm thêm (phút)', '加班（分钟）', 'បន្ថែមម៉ោង (នាទី)'),
        'night_minutes': ('Night (minutes)', 'Ban đêm (phút)', '夜间（分钟）', 'ពេលយប់ (នាទី)'),
        'holiday_minutes': ('Holiday (minutes)', 'Ngày nghỉ (phút)', '休息日（分钟）', 'ថ្ងៃឈប់សម្រាក (នាទី)'),
        'note': ('Note', 'Ghi chú', '备注', 'កំណត់សម្គាល់'),
    },
    'earning': _ITEM_COLUMNS,
    'deduction': _ITEM_COLUMNS,
    'transaction': {
        'date': ('Transaction date', 'Ngày giao dịch', '交易日期', 'ថ្ងៃប្រតិបត្តិការ'),
        'time': ('Transaction time', 'Giờ giao dịch', '交易时间', 'ម៉ោងប្រតិបត្តិការ'),
        'description': ('Transaction type', 'Loại giao dịch', '交易类别', 'ប្រភេទប្រតិបត្តិការ'),
        'sender': ('Sender shown', 'Người gửi hiển thị', '汇款人显示', 'អ្នកផ្ញើដែលបង្ហាញ'),
        'deposit_amount': ('Deposit amount (KRW)', 'Số tiền gửi vào (KRW)', '入账金额（韩元）', 'ចំនួនប្រាក់បញ្ញើ (វ៉ុន)'),
        'withdrawal_amount': ('Withdrawal amount (KRW)', 'Số tiền rút ra (KRW)', '出账金额（韩元）', 'ចំនួនប្រាក់ដក (វ៉ុន)'),
        'balance': ('Balance after transaction (KRW)', 'Số dư sau giao dịch (KRW)', '交易后余额（韩元）',
                    'សមតុល្យក្រោយប្រតិបត្តិការ (វ៉ុន)'),
        'reference': ('Transaction reference', 'Mã giao dịch', '交易标识', 'លេខយោងប្រតិបត្តិការ'),
    },
}
_ORDER = ('en', 'vi', 'zh', 'km')
_ROW_KEY = re.compile(r'^(work|earning|deduction|transaction)_(\d+)_([a-z_]+)$')


def _korean_fields():
    names = {key: spec['label'] for key, spec in ROLE_FIELDS.items()}
    for profile in PROFILES.values():
        names.update({key: spec['label'] for key, spec in profile['fields'].items()})
    return names


def _korean_rows():
    rows, columns = {}, {}
    for profile in PROFILES.values():
        for name, row in profile['rows'].items():
            rows[name] = row['label']
            columns[name] = {key: spec['label'] for key, spec in row['fields'].items()}
    return rows, columns


KOREAN_FIELDS = _korean_fields()
KOREAN_ROWS, KOREAN_COLUMNS = _korean_rows()


def _pick(korean, others, language):
    if language == 'ko' or others is None:
        return korean
    return dict(zip(_ORDER, others)).get(language, korean)


def field_label(key, language):
    """Display name of a known field code, or None when the code is not a known field."""
    if key in KOREAN_FIELDS:
        return _pick(KOREAN_FIELDS[key], _FIELDS.get(key), language)
    match = _ROW_KEY.match(key or '')
    if match and match.group(3) in KOREAN_COLUMNS.get(match.group(1), {}):
        row, number, column = match.groups()
        return '{} {} · {}'.format(_pick(KOREAN_ROWS[row], _ROWS.get(row), language), number,
                                   _pick(KOREAN_COLUMNS[row][column], _COLUMNS[row].get(column), language))
    return None


def localize_fields(fields, language, localizer):
    """Replace display names; extra items named by the AI go through the cached translator."""
    fields = [dict(f) for f in fields]
    extras = []
    for f in fields:
        key = f.get('agentKey') or f.get('key')
        label = field_label(key, language)
        if label is not None:
            f['label'] = label
        elif str(key).startswith('extra_') and f.get('label'):
            extras.append(f)
    if extras and language != 'ko':
        for f, label in zip(extras, localizer.translate([f['label'] for f in extras], language)):
            f['label'] = label
    return fields
