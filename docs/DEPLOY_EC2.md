# 통합본 EC2 HTTP 배포

작성일: 2026-11-11 (Asia/Seoul). 대상은 `woowangthon/WOOWANGTHON_TEAM_49_BE` 통합 저장소다. **이 저장소 루트에서** 실행하며 `devmerge`로 이동하지 않는다. EC2 생성·SSH·Docker 설치가 완료된 상태에서 시작한다. 도메인이나 Caddy 없이 퍼블릭 IP:8000을 사용한다.

## 1. 배포 파일 확인

EC2의 클론한 폴더에서 다음 파일이 보여야 한다.

```bash
ls Dockerfile compose.yaml .env.production.example
sudo docker compose version
```

이 배포 구성은 로컬에서 추가했다. 원격에 아직 push하지 않았다면 먼저 로컬 main의 배포 커밋을 push한 뒤 EC2에서 같은 브랜치를 갱신한다. 이미 clone한 폴더를 다시 clone할 필요 없다.

```bash
git status --short
git pull --ff-only
```

서버에서 수정한 파일이 있으면 내용을 먼저 확인한다. reset/clean으로 덮어쓰지 않는다.

## 2. 환경파일 작성

새 파일일 때 복사한다. 이미 `.env.production`이 있으면 그 파일을 편집하여 키를 유지한다. `.env`는 로컬 Python 실행용이고, 아래 Docker 구성은 `.env.production`을 컨테이너 환경변수로 전달한다.

```bash
cp -n .env.production.example .env.production
chmod 600 .env.production
nano .env.production
```

중요한 항목:

```dotenv
API_IMAGE=woowangthon-api:local
API_BIND_HOST=0.0.0.0
API_PORT=8000
CORS_ORIGINS=["http://localhost:3000","http://프론트IP:3000"]
FRONTEND_BASE_URL=http://프론트IP:3000
COOKIE_SECURE=false
WORKER_CONCURRENCY=1
OPENAI_API_KEY=실제키
OPENAI_EXTRACTION_MODEL=팀에서_확인한_모델ID
OPENAI_EXPLANATION_MODEL=팀에서_확인한_모델ID
OPENAI_CHAT_MODEL=팀에서_확인한_모델ID
```

설명:

| 값 | 설정 방법 |
| --- | --- |
| `API_BIND_HOST` | 외부 HTTP 접속은 `0.0.0.0` |
| `API_PORT` | 외부 포트. 현재 8000; 보안 그룹·프론트 URL도 맞춘다 |
| `CORS_ORIGINS` | **브라우저에서 여는 프론트 주소** 목록. 백엔드 주소 아님. 프로토콜·호스트·포트까지 일치, 경로/끝 슬래시 제외 |
| `FRONTEND_BASE_URL` | 실제 프론트 페이지 주소. 80포트이면 `http://프론트IP`, 3000이면 `http://프론트IP:3000` |
| `COOKIE_SECURE` | 이번 HTTP 시연은 `false` |
| `OPENAI_API_KEY` | 서버에서만 입력. Git이나 프론트에 전달하지 않음 |
| 모델 3개 | 팀이 해당 OpenAI 프로젝트에서 확인한 모델 ID. 예제 기본 모델의 접근권한은 검증하지 않았음 |

아직 프론트 주소를 모르면 템플릿의 localhost origin으로 서버부터 확인하고, 주소가 나오면 추가한 뒤 컨테이너를 재생성한다. `API_DOMAIN`은 사용하지 않는다. `HOST/PORT/DATA_DIR/폰트/OCR` 경로는 Compose가 컨테이너용 값으로 고정한다. 서버에 Python·OCR·DB 서버를 따로 설치할 필요 없다.

키 없이도 서버 기동·디지털 PDF 읽기·확인 입력 계산·템플릿 설명·PDF 생성이 가능하다. 실제 추출 A/보완 B/자유 채팅과 새 검토 작업실의 자동 분류·추출·이미지 검증에는 유효한 모델 설정이 필요하다. 운영 경로에 가짜 성공 응답은 없다.

## 3. 빌드하고 실행

```bash
sudo docker compose --env-file .env.production config --quiet
sudo docker compose --env-file .env.production up -d --build --wait api
```

EC2에서 Docker 이미지를 빌드하고 그 이미지로 컨테이너를 띄운다. 이미지에는 Python 3.14·`uv.lock` 고정 의존성·Tesseract kor/eng·한글 폰트를 포함한다. 별도 agent 서버나 Docker Hub 업로드가 필요하지 않다. 단일 Uvicorn 프로세스로 실행한다. `WORKER_CONCURRENCY`는 내부 작업 동시성이고 프로세스 개수가 아니다.

## 4. 서버 내부에서 확인

```bash
sudo docker compose --env-file .env.production ps
curl --fail http://127.0.0.1:8000/health
sudo docker compose --env-file .env.production logs --tail=80 api
```

health의 `data.status=ok`, `data.aiMode=direct`면 기동 성공이다. 실제 외부 모델 인증·추출 품질까지 확인하는 것은 아니다. OCR 언어는 다음으로 확인한다.

```bash
sudo docker compose --env-file .env.production exec -T api tesseract --list-langs
```

`kor`, `eng`가 있어야 한다. 새 검토 작업실 경로는 Swagger의 `Review workspaces`에서 확인한다.

## 5. 보안 그룹과 외부 접속

현재 구성은 TCP 22(본인 관리 IP)와 TCP 8000이 필요하다. 8000은 처음 본인 IP/32로 점검하거나 공개 시연 시 `0.0.0.0/0`으로 허용한다. 일반 API 요청은 사용자 브라우저가 보내므로 프론트 서버 IP만 허용하면 다른 사용자는 접근할 수 없다. 이번 구성에서 80·443은 필요하지 않다.

본인 컴퓨터의 브라우저에서:

```text
http://백엔드퍼블릭IP:8000/health
http://백엔드퍼블릭IP:8000/docs
```

프론트 팀에 전달할 API base URL:

```text
http://백엔드퍼블릭IP:8000/api/v1
```

내부 curl은 되는데 외부 접속이 안 되면 EC2 보안 그룹·퍼블릭 IP/라우팅·OS 방화벽·`API_BIND_HOST` 설정을 확인한다. 인스턴스를 중지했다가 시작해서 IP가 바뀌면 프론트 주소도 갱신한다.

프론트 페이지도 HTTP로 열어야 HTTP API를 브라우저에서 직접 호출할 수 있다. HTTPS 페이지의 HTTP fetch는 혼합 콘텐츠로 차단된다. HTTP는 통신을 암호화하지 않으므로 실제 사용자 자료 운영 전에 HTTPS를 적용한다.

현재 refresh 쿠키는 `SameSite=Lax`다. 프론트와 API가 서로 다른 사이트/IP라면 CORS와 `COOKIE_SECURE=false`만으로 브라우저 refresh가 보장되지 않는다. 같은 origin의 프론트 프록시 또는 HTTPS/쿠키 정책 설계를 별도로 맞춘다. 로그인 후 Bearer API 호출과 구분한다.

## 6. 실제 API 흐름 확인 — 선택

서버에서 실행하면 합성 계정/자료를 만들고 PDF 읽기→새 검토 초안/판독/페이지 PNG→확인 입력→실제 agent 검사→설명→한글 보고서 다운로드→문의 작성까지 점검한다. 유료 모델 호출은 없으며 자료가 남으므로 health probe로 반복 실행하지 않는다.

```bash
sudo docker compose --env-file .env.production exec -T api python - < scripts/deployment_smoke.py
```

외부 접속 경로도 확인하려면 의존성을 설치한 개발 머신에서:

```bash
uv run python scripts/deployment_smoke.py --base-url http://백엔드퍼블릭IP:8000
```

## 7. 수정·종료·데이터

환경파일만 변경했을 때:

```bash
sudo docker compose --env-file .env.production up -d --force-recreate --wait api
```

코드 변경 후:

```bash
git pull --ff-only
sudo docker compose --env-file .env.production up -d --build --wait api
```

종료:

```bash
sudo docker compose --env-file .env.production down
```

현재 SQLite·원본·새 검토 업로드·산출물은 EBS 위 Docker 볼륨 `woowangthon_app_data`에 저장된다. 컨테이너 재생성 시 유지하며 이전 `devmerge` 볼륨을 자동 복사하지 않는다. `down -v`는 데이터 볼륨까지 삭제하므로 사용하지 않는다. 코드/DB 업데이트 전에는 전체 데이터 볼륨을 백업하고, EC2/EBS 삭제는 볼륨 영구 저장과 별개임을 유의한다.

실제 로컬 검증 결과는 [DOCKER_VERIFICATION.md](DOCKER_VERIFICATION.md)에 기록한다. 로컬 검증과 실제 EC2 기동·프론트 연동·유료 모델 성공은 별개의 확인이다.
