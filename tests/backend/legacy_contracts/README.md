# 이전 백엔드 계약 테스트 보존

이 디렉터리는 원본 백엔드 테스트의 복사본을 보존한다. 파일명만 기본 pytest 검색에서 제외되도록 변경했으며 `tests.conftest` import는 통합 위치인 `tests.backend.conftest`로 변경했다. 원본 폴더에는 어떤 변경도 하지 않았다.

기존 HTTP `/analyze`·`/chat` 어댑터, 기본 mock 성공, 업로드만으로 자동 AI 호출, 수기 저장 즉시 구 백엔드 계산식 실행, 네 종류 독립 계산 스냅샷은 이번 사용자 요구에 따라 폐기된 계약이다. 이를 통과시키기 위해 운영 경로에 HTTP 서버나 가상 성공 응답을 되살리지 않는다.

처음 실행한 기록은 `docs/backend-regression-original.txt`이며 `--maxfail=10`에서 10 실패·1 통과로 중단했다. 실행하지 않은 나머지는 통과로 주장하지 않는다. 유지된 인증·권한·배분·DB·입력 검사 10개는 `../test_retained.py`에서 원래 함수를 다시 수집한다. 새 실행 계약은 `tests/integration/`에서 실제 AgentPorts/run_task/규칙/저장 경로로 검증한다.

개별 이전 계약 재현 예시(실패 가능):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/backend/legacy_contracts/original_ai.py -q
```
