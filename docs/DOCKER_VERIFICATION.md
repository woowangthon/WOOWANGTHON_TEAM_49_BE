# 통합 저장소 Docker 검증

검증일: 2026-11-11 (Asia/Seoul). 대상은 새 통합 저장소 `WOOWANGTHON_TEAM_49_BE`의 루트다. 예전 devmerge 검증 결과를 이 코드의 새 전체 회귀 실행으로 간주하지 않는다.

| 검사 | 결과 |
| --- | --- |
| 이미지 빌드 | 최신 app/agent로 `linux/amd64` 이미지 `woowangthon-api:verify-20261111` 성공 |
| Compose 환경 | HTTP 템플릿의 공개 바인딩 0.0.0.0:8000·COOKIE_SECURE=false·영구 데이터 경로 렌더링 확인 |
| 실제 기동 | 별도 localhost 포트 18084·새 볼륨, 초기 DB 생성 및 health 성공 |
| 기존 API 실제 흐름 | 합성 가입/로그인→PDF 읽기→확인 입력→agent 검사→설명→한글 PDF 다운로드→문의 통과 |
| 새 검토 작업실 실제 흐름 | auto_process/auto_verify=false로 초안 생성→PDF 업로드→worker 판독 COMPLETED→페이지 PNG 미리보기 통과 |
| 생성 파일 | PDF 44,093바이트, 검토 PNG 24,167바이트; 보고서에 기준 기간과 2,000,000원 텍스트 확인 |
| OCR 설치 | 컨테이너 Tesseract 언어 eng/kor/osd 확인 |
| 정적 검사 | 추가 deployment_smoke.py Ruff 통과 |

실제 HTTP smoke는 `scripts/deployment_smoke.py`를 컨테이너 Python에 전달하여 실행했다. 이는 외부 모델 호출 0회인 합성 점검이며 실제 추출 A/B/자유 채팅·새 검토 자동 모델 처리를 검증한 것이 아니다. 실제 EC2 접속·보안 그룹·프론트 브라우저 쿠키 연동·부하·스캔 OCR 정확도는 검증하지 않았다. 기존 전체 회귀 기록은 [VERIFICATION.md](VERIFICATION.md)에 보존했다. 이번 배포 구성 추가로 앱 런타임 코드를 바꾸지 않았으며 전체 회귀를 새로 실행하지 않았다.

이미지는 macOS arm64의 Docker Desktop에서 amd64 대상으로 빌드/실행했다. Docker의 PASSWORD_RESET_DELIVERY 변수명 경고는 disabled 설정에 대한 이름 기반 경고이며 실제 API키/비밀번호를 이미지에 넣지 않았다. 비밀 환경파일과 DB/원본·테스트·개발용 UI는 이미지 컨텍스트에서 제외했다.

재현은 [EC2 HTTP 배포 가이드](DEPLOY_EC2.md)를 따른다. 검증용 컨테이너는 종료하며 빌드 이미지와 합성 자료 볼륨을 유지한다.
