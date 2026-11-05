# control-plane

![control-plane 아키텍처](docs/architecture.png)

> 파란 선은 제어 명령, 회색 선은 상태·근거·트래픽입니다. 양방향 화살표는 앱의 heartbeat와 플랫폼의 업무 검사입니다. [인터랙티브 버전](https://claude.ai/artifact/QMfrqiTnyDfViBgicQxv9i)

Foxtrot 플랫폼 백엔드입니다. 세 이전 서버(DB, Redis, 오브젝트)와 Cloudflare 가중치를 정해진 순서로 제어하고, 콘솔에 진행 상황과 제어 수단을 제공합니다.

- 설계: [플랫폼 백엔드 설계 초안](https://app.notion.com/p/40f8bee9ada48174b929d433081f41b6)
- 공통 API: [마이그레이션 공통 Design Doc](https://app.notion.com/p/40d8bee9ada4815e9e94ee454be82d4a). `tests/contract/common-api.json`은 `db-migration-adaptor@1eac972`의 계약 사본입니다.
- 앱이 구현할 내용: [docs/app-integration.md](docs/app-integration.md)

## 구조

```txt
src/control_plane/
  domain/       저장하는 모델과 전환 순서
  store/        DynamoDB·메모리 테이블, 키 구조를 아는 유일한 곳인 Repository
  adapters/     client: 공통 API 호출 / profiles: 어댑터별 의미 차이
  engine/       commands: 명령 1회 효과 보장 / stages: 단계 핸들러
                recovery: 롤백·중단·대조 / engine: 틱 루프
  workload/     앱 업무 정지·재개와 실제 정지 확인
  dbgate.py     DB 이전 서버의 정지 근거 기록 (SDK를 아는 유일한 곳)
  traffic.py    Cloudflare 가중치 (Cloudflare를 아는 유일한 곳)
  probes.py     실제 앱 경로 업무 검사와 카나리 관측
  workflows.py  사람이 내리는 명령과 허용 상태
  auth.py       GitHub 로그인과 세션
  api/          HTTP 표면
  main.py       조립과 실행
```

### 지켜야 할 규칙

1. **엔진은 상태를 메모리에 두지 않습니다.** 틱마다 읽고 한 가지를 수행한 뒤 저장합니다. 어느 두 틱 사이에 프로세스가 죽어도 이어서 진행합니다.
2. **외부 변경 전에 의도를 먼저 기록합니다.** 응답을 잃으면 저장한 키와 본문을 그대로 재전송해 원래 작업을 찾습니다.
3. **리소스별 차이는 `profiles.py`에만 둡니다.** 새 리소스는 프로필 하나를 추가합니다. 엔진에 `if redis:`를 넣지 않습니다.
4. **판정에 `phase` 문자열을 쓰지 않습니다.** 분기는 `lifecycle`, `health`, `readiness`, `checks`, `details`로 합니다.
5. **모르면 멈춥니다.** 결과가 불명이면 RECONCILE 전까지 다음 변경을 보내지 않습니다. 자동으로 실행하는 되돌리기는 데이터 경로를 바꾸지 않는 트래픽 가중치뿐입니다.
6. **시간은 `Clock`으로만 읽습니다.** 그래서 시간 관련 동작을 실제로 기다리지 않고 시험합니다.

## 로컬 실행

요구 사항은 [uv](https://docs.astral.sh/uv/)입니다. Python 3.13은 uv가 설치합니다.

```bash
make setup
```

```bash
cp config/seed.example.yaml config/seed.yaml
```

`config/seed.yaml`을 환경에 맞게 고치고 `.env.example`을 `.env`로 복사합니다. 로컬에서는 메모리 저장소와 로그인 생략을 쓸 수 있습니다. 이 두 설정은 `CONTROL_PLANE_ENVIRONMENT=local`에서만 허용합니다.

```bash
make run
```

`platform-frontend`의 개발 서버가 `/api`를 `127.0.0.1:8000`으로 전달하므로 그대로 연결됩니다. API 문서는 <http://127.0.0.1:8000/docs>에 있습니다.

## 검사

```bash
make check
```

ruff, mypy strict, pytest를 실행합니다. 테스트는 외부 서비스 없이 동작합니다. DynamoDB 동작은 moto로 메모리 구현과 같은 테스트를 통과시킵니다. `tests/fakes.py`의 가짜 어댑터는 실제 어댑터에서 확인한 함정을 재현하고, 응답은 팀 계약으로 검증합니다.

### 실제 어댑터 검증

가짜를 상대로 한 통과는 실제 어댑터와의 호환을 증명하지 못합니다. `tests/integration`은 팀원의 실제 코드를 대상으로 실행하며, 환경 변수가 없으면 건너뜁니다.

```bash
uv pip install -e ../db-migration-adaptor
```

```bash
OBJECT_MIGRATION_REPO=../object-migration STATEMIG_URL=http://localhost:18090 uv run --no-sync pytest tests/integration
```

Redis 스택은 `redis-migrator`에서 `docker compose -f demo/docker-compose.yml up -d --build`로 띄웁니다.

현재 실행 상태: `tests/integration`은 아직 실행 전입니다. 가짜 어댑터 테스트는 실제 코드를 읽고 맞춘 것이므로, 첫 연동 때 이 테스트부터 돌립니다.

## 알아 둘 동작

- **DB 보존 기간:** DB 어댑터는 `RESUME` 후 보존 기간(기본 3600초)이 지나야 FINALIZE를 받습니다. 시연에서는 어댑터 설정을 줄입니다. 그동안 7단계는 30초마다 다시 시도합니다.
- **Cloudflare 가중치 단위:** `default_weight`는 0.1 단위로 문서화되어 있고 `pool_weights`의 단위는 확인되지 않았습니다. 기본값 0.1에서는 95:5가 약 91:9로 적용되며 실제 비율을 기록합니다. 더 작은 단위가 허용되는 것을 확인하면 `CONTROL_PLANE_CLOUDFLARE_WEIGHT_STEP`을 낮춥니다.

## 배포

ECS 태스크 하나로 실행합니다. Worker를 늘리지 않습니다. 실행 주체가 하나라는 전제로 설계했습니다.

```bash
make image-build
```

DB 전환을 쓰려면 `db_operator` 패키지가 필요합니다. 비공개 저장소이므로 이미지 빌드 시 `github_token` 시크릿으로 설치합니다.

```bash
docker build --secret id=github_token,env=GITHUB_TOKEN --build-arg DB_OPERATOR_REF=1eac972 -t foxtrot/control-plane .
```

### DynamoDB 테이블

| 속성 | 값 |
| --- | --- |
| 파티션 키 | `pk` (String) |
| 정렬 키 | `sk` (String) |
| 과금 | On-demand |

### 작업 역할 권한

- 플랫폼 테이블의 `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`, `Query`
- DB 이전 서버 테이블의 제어 영역 쓰기
- `secretsmanager:GetSecretValue`: 시드와 설정에 있는 시크릿만

앱의 DB, Redis, 버킷에는 권한을 주지 않습니다. 플랫폼은 저장소를 직접 다루지 않습니다.

## 설계와 다르게 구현한 부분

- **`HALT` 명령 추가:** 실행 중인 워크플로를 사람이 멈출 수 있도록 설계의 명령 목록에 더했습니다.
- **`ABORTING` 상태 추가:** 중단 중에도 CANCEL이 비동기로 진행되므로 필요합니다.
- **`COMMAND#` 키:** 설계는 멱등 키였지만 `{kind}#{slot}#{attempt}`로 바꿨습니다. 키는 revision에 따라 달라져 조회할 수 없고, RESUME이 정방향과 복귀에 한 번씩 실행됩니다.
- **잠금 항목 분리:** 잠금 갱신으로 콘솔의 `expectedVersion`이 계속 어긋나지 않도록 `META` 밖의 `LEASE`로 옮겼습니다.
- **HALTED와 승인 대기 중에도 업무 검사 지속:** 사람이 판단할 때 앱 상태를 알아야 하고, DB 어댑터도 신선한 근거를 요구합니다.
- **RUNNING 근거를 단계 밖에서 유지:** DB 어댑터는 `readiness.advance`뿐 아니라 FINALIZE 시점에도 30초 이내의 근거와 30초 안정 구간을 요구합니다. 그래서 `RunningEvidence`가 DB 전환 후 FINALIZE까지 계속 기록합니다.
- **중단 조건 강화:** 전환이 진행 중인 리소스가 있으면 ABORT를 거절하고 업무 정지를 유지합니다. 멈춘 프록시 앞에서 앱을 재개하는 사고를 막기 위해서입니다.
- **Redis failure 복귀 조건:** 대상을 직접 볼 권한이 없으므로 사람의 확인과 최신 업무 검사 실패를 함께 요구합니다.
