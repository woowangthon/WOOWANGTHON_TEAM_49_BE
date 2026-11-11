# 작업 기록

## 2026-11-11 — 새 통합 저장소 HTTP Docker 배포

- 사용자 요청: 실제 통합 리포 기준 환경파일부터 Docker 기동까지 안내. 대상은 WOOWANGTHON_TEAM_49_BE 루트(main)이며 devmerge 폴더가 없음.
- Dockerfile/.dockerignore/compose.yaml/.env.production.example 추가. Python 3.14·잠금 의존성·한국어 OCR·한글 폰트·비루트 실행·healthcheck·영구 볼륨 포함. 퍼블릭 IP:8000 HTTP 설정 제공.
- README의 devmerge 실행 경로를 저장소 루트로 정정. 로컬 .env.example의 없는 assets/tessdata 경로는 선택적 실제 설치 경로 예시로 변경.
- EC2의 환경파일 작성→빌드/기동→상태/로그 확인→외부/프론트 연결→설정/코드 갱신 문서 작성. API_DOMAIN/Caddy 미사용.
- 최신 통합본으로 linux/amd64 이미지 실제 빌드 및 HTTP smoke 통과. 기존 PDF/agent/보고서 흐름과 새 검토 초안·비동기 판독·페이지 PNG까지 실제 컨테이너에서 확인. 유료 모델 호출 0회.
- HTTP 템플릿의 포트/쿠키/볼륨 렌더링, 한국어/영어 OCR 설치, 추가 스크립트 Ruff 통과. 앱 런타임 변경 없어 전체 회귀 재실행은 하지 않음.
- 세부 결과는 docs/DOCKER_VERIFICATION.md. 실제 원격 push·EC2 배포·유료 모델·프론트 쿠키 연동은 미실행.

## 2026-11-11 — 첨부 프론트 API 계약·에이전트 통합 전체 검증

- 사용자 요청: 첨부 API 설계안/이전 프론트 연동 명세를 기준으로 전체 테스트·통신·업무 로직·backend→agent 호출 검증.
- 격리된 임시 복사본/합성 DB에서 전체 활성 테스트 733 passed / 9 skipped / 13 warnings, 30.60초. Ruff·컴파일 통과. 외부 유료 모델은 호출하지 않음.
- 현재 OpenAPI 98 path/133 HTTP 작업이 저장된 export와 일치. 첨부 B-01~92 중 87개 등록, 기존 reports 5개 누락. TestClient 및 실제 Docker TCP에서 92개 route/auth 탐침 수행. 성공 DTO 전체 전수 통과라고 해석하지 않음.
- 38개 비교 점검 중 현재 지원 경로 10개 통과, 명세 차이 28항목(이전 연동 24·미합의 v0.5 제안 4). 독립 버그 개수와 다름.
- 자동 검사 중단/새 비교 묶음 확인 필요, 기존 fields PATCH 409, READ_READY/필드 타입 변경, results/recompute DTO·독립 버전·검사 선택·문의 body 무시·보고서 경로 누락 확인.
- 실제 추가 문제: 50페이지 허용 업로드에서 21페이지 텍스트 PDF가 202 후 DOCUMENT_UNREADABLE 실패. agent max_pages=20과 불일치. 진단 사본의 max_pages=50에서는 같은 바이트 21페이지 판독 성공. 운영 코드는 미변경.
- 실제 runner를 감싼 관찰 wrapper로 extract_document/analyze_case/explain_result/build_report 완료 추적. R-05 차액 10,000원 및 PDF/ZIP 바이트 일치 확인. 새 검토 작업실·기존 계산/보고서 HTTP smoke 성공.
- 실제 TCP CORS/Bearer/동일 origin 쿠키·refresh·업로드 READ_READY·키 없는 SSE error 및 FAILED 저장 확인. 외부 프론트 브라우저·EC2·실제 모델 품질은 미검증.
- scripts/audit_frontend_contract.py와 92개 참조 fixture·원시 JSON/회귀 로그·endpoint matrix·FRONTEND_CONTRACT_AUDIT.md 추가. --strict는 차이 발견 시 종료 코드 1이며 현재 정상적으로 계약 불합격을 나타냄.
- 앱 런타임 수정 없이 검증 코드/기록만 추가. 호환 adapter 또는 프론트 계약/호출 순서 동시 개정이 필요하다는 결과를 남김.


## 2026-11-12 — 프론트 계약 오류 수정·기존 Case API 복구

- 사용자 요청: 로컬 프론트 부재는 실패로 취급하지 않고 첨부 API 계약 기준으로 오류 수정. 실제 통합 저장소 woowangthon-be 루트 대상으로 격리된 사본에서 구현 후 반영.
- 기존 수기·확인 필드·조건·배분을 소유권/원본 출처를 가진 agent InputSnapshot으로 고정하고 실제 run_task에 전달. 저장 후 영속 자동 검사, checks 전달, 검사별 정수 snapshot/독립 버전, ResultsDTO·recompute-status·readiness 복구.
- legacy fields PATCH의 후보 확인·수기 정정·Case override·공유값·재추출 후 재확인 지원. 숫자형 공개 키와 원시 agentKey를 병행하고 잘못된 금액·소수 절삭을 막음. PDF 판독 한도 20→50으로 업로드와 일치.
- 기존 보고서 5개 경로를 실제 분석→설명→보고서 파이프라인에 연결. 입력·원본 고정, idempotency, 재시작 단계 복구, PDF/ZIP 동일 바이트, 연결 철회 후 접근 차단 확인. 원본 ZIP에 result-snapshot.json 별칭 추가.
- 문의문 선택 항목/참조 버전 검증·실제 선택 내용 반영. 채팅 clientMessageId 중복 저장/호출 방지, 재생 시 출처 재검사, page 쿼리로 페이지 응답 제공(기본 messages 배열 유지). 새 비교 묶음·검토 작업실 흐름 유지.
- 최종 전체 회귀 752 passed / 9 skipped / 13 warnings, 32.96초. Ruff·컴파일 통과. 첨부 92개 경로 모두 등록, 계약 비교 38/38 일치. OpenAPI 102 path/138 HTTP 작업, 빈 JSON 성공 스키마 0(일부 동적 응답은 JsonValue envelope).
- linux/amd64 Docker 이미지 빌드 및 실제 HTTP의 기존/신규 흐름 성공. 유료 모델 호출 0회, 사용자 DB·파일 미사용. 프론트 브라우저·원격 EC2 배포·실제 모델 품질은 검증하지 않음.
- 상세: docs/FRONTEND_CONTRACT_FIXES.md, contract-audit/fixed-* 증거. 이전 감사는 수정 전 기록으로 표시해 보존. 원격 push는 수행하지 않음.
