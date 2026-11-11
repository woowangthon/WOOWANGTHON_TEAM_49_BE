"""Small sourced seed. No external request or document transmission at lookup time."""
from app.db import dump


def seed_centers(con):
    # Official pages checked 2026-11-08; languages/visit details are deliberately unverified.
    center = {'centerId': 1, 'name': '고용노동부 고객상담센터', 'region': '전국', 'categories': ['WAGE', 'LABOR'],
              'phone': '1350', 'website': 'https://1350.moel.go.kr/home/', 'businessHours': '평일 09:00~18:00',
              'languages': None, 'visitInformation': None, 'sourceUrl': 'https://1350.moel.go.kr/home/hp/ch/callchat.do',
              'verifiedAt': '2026-11-08', 'dataSource': 'VERIFIED_DB', 'isNationwide': True,
              'notes': '전국 단일번호, 유료 전화상담. 지역 기관 데이터와 정기 갱신은 추후 연동.'}
    con.execute('INSERT OR IGNORE INTO consultation_centers(id,data_json) VALUES(?,?)', (1, dump(center)))
