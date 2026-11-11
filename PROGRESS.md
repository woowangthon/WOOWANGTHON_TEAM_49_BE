# 작업 기록

## 2026-11-11 — 새 통합 저장소 HTTP Docker 배포

- 사용자 요청: 실제 통합 리포 기준 환경파일부터 Docker 기동까지 안내. 대상은 WOOWANGTHON_TEAM_49_BE 루트(main)이며 devmerge 폴더가 없음.
- Dockerfile/.dockerignore/compose.yaml/.env.production.example 추가. Python 3.14·잠금 의존성·한국어 OCR·한글 폰트·비루트 실행·healthcheck·영구 볼륨 포함. 퍼블릭 IP:8000 HTTP 설정 제공.
- README의 devmerge 실행 경로를 저장소 루트로 정정. 로컬 .env.example의 없는 assets/tessdata 경로는 선택적 실제 설치 경로 예시로 변경.
- EC2의 환경파일 작성→빌드/기동→상태/로그 확인→외부/프론트 연결→설정/코드 갱신 문서 작성. API_DOMAIN/Caddy 미사용.
- 최신 통합본으로 linux/amd64 이미지 실제 빌드 및 HTTP smoke 통과. 기존 PDF/agent/보고서 흐름과 새 검토 초안·비동기 판독·페이지 PNG까지 실제 컨테이너에서 확인. 유료 모델 호출 0회.
- HTTP 템플릿의 포트/쿠키/볼륨 렌더링, 한국어/영어 OCR 설치, 추가 스크립트 Ruff 통과. 앱 런타임 변경 없어 전체 회귀 재실행은 하지 않음.
- 세부 결과는 docs/DOCKER_VERIFICATION.md. 실제 원격 push·EC2 배포·유료 모델·프론트 쿠키 연동은 미실행.
