# DB 에이전트 연결

DB도 Redis처럼 공통 API로 계획을 등록하고 명령 결과를 조회합니다. 기본 DB 프로필은 `postgres-managed-logical-pgbouncer-v2`입니다. 초기 복사와 CDC는 PostgreSQL이 수행합니다. DMS 작업과 Promote는 호출하지 않습니다.

## 변경 범위

1. DB 계획과 지원 기능 검사에 직접 논리 복제 프로필을 사용합니다.
2. DB와 업무 범위를 등록할 때 DMS 작업 등록을 생략합니다.
3. 결과가 불명확하면 제한된 횟수로 RECONCILE을 실행하고, 확인하지 못하면 멈춥니다.
4. 기존 DMS는 프로필을 명시한 설정에서만 사용합니다. 실패 시 복제 방식을 자동 변경하지 않습니다.

## 실행 구조

```text
오케스트레이터 → DB Agent API → DynamoDB → Worker
앱 → 상시 PgBouncer → 현재 DB
                         원본 DB → 논리 복제 → 대상 DB
```

DB 쪽 ECS Task는 하나입니다. API와 Worker를 한 컨테이너에 두고, PgBouncer를 다른 컨테이너에 둡니다. 여러 앱은 PgBouncer의 DB별 alias로 접속합니다.

`PREPARE → START_SYNC → QUIESCE → CUTOVER → RESUME` 순서로 호출합니다. Redis와 같이 `QUIESCE`는 준비 확인, `CUTOVER`는 DB 쓰기 정지·전환·재개, `RESUME`은 결과 확인입니다. `ROLLBACK`도 원본 DB 쓰기 재개를 포함합니다.

오케스트레이터는 `QUIESCE` 뒤, `CUTOVER` 직전에 실제 앱·업무 Worker의 정지를 확인하고 `quiesceRef`를 발급·갱신합니다. DB 쓰기 경로가 열린 것을 `RESUME`으로 확인한 뒤 앱 업무를 재개합니다. DB의 `CUTOVER`·`ROLLBACK`에는 실제 HELD 근거가 필요합니다. 확인 전용 명령에 쓰는 참조로 이 검사를 우회할 수 없습니다.

기본 정책 `BEFORE_TARGET_WRITE`는 대상 쓰기 전 원본 복귀만 허용합니다.
DB v2 에이전트가 역방향 CDC를 구현한 경우 아래 설정으로 대상 쓰기 후 복귀를 요청할 수 있습니다.

## 역방향 CDC 연동

```yaml
resources:
  database:
    profile: postgres-managed-logical-pgbouncer-v2
    policy: {rollbackMode: REVERSE_REQUIRED, maxQuiesceSeconds: 300}
    # sourceRef, targetRef, catalog 등은 기존 설정 유지
```

새 HTTP 경로는 필요하지 않습니다. 기존 `POST /v1/workflows/{id}/commands`에
`command: ROLLBACK_RESOURCE`, `resourceId: DATABASE`, `mode: normal`, 최신 `expectedVersion`을
보냅니다. 실행 중이면 먼저 `HALT`합니다. 완료된 워크플로나 FINALIZE가 끝난 리소스에 대한
복귀를 새로 허용하는 변경은 아닙니다.

컨트롤플레인과 DB 에이전트는 기존 공통 API 계약을 다음과 같이 사용합니다.

- `/v1/capabilities`의 **선택한 v2 프로필**에 `kind: DATABASE`,
  `rollbackModes: [..., REVERSE_REQUIRED]`, `actions: [..., ROLLBACK]`이 있어야 합니다.
  없으면 워크플로 생성 전에 `UNSUPPORTED_ROLLBACK_MODE`로 거절합니다. v1과 DMS에는 허용하지 않습니다.
- 계획의 `policy.rollbackMode`를 그대로 전달합니다. 역방향 복제의 준비·시작·유지·정리는
  에이전트가 기존 전환 명령과 FINALIZE 안에서 수행합니다. 별도 CDC 시작 명령은 보내지 않습니다.
- 에이전트는 복귀 진입이 가능한 상태에서 `allowedActions`에 `ROLLBACK` 또는 사전 확인용
  `QUIESCE`를 제공합니다. 컨트롤플레인은 앱과 Worker를 HELD로 확인하고 새 정지 근거를 게시한 뒤
  `readiness.rollback: PASS`, `health: OK`, 실행 중인 작업 없음, `ROLLBACK` 허용을 확인합니다.
  `FAIL`이면 차단 이유를 남기고 멈춥니다. UNKNOWN·누락은 `command_timeout`까지 기다린 뒤 멈춥니다.
  기다리는 동안 정지 근거를 갱신하며, 이 단계의 실패나 시간 초과에는 앱을 자동 재개하지 않습니다.
- `ROLLBACK`에는 기존 `transitionId`, 최신 `quiesceRef`, `expectedRevision`과 멱등 키를 보냅니다.
  Redis 전용 `mode` 필드는 DB 에이전트에 보내지 않습니다. 대상 쓰기 차단, 마지막 변경까지 원본 반영,
  복제 방향 정리, sequence와 DB 경로·쓰기 권한 복귀는 에이전트가 수행하고 안전성을 다시 검사해야 합니다.
  준비 상태 PASS나 복제 지연 0만으로 최종 동기화가 끝났다고 간주해서는 안 됩니다.
- 기존 v2 계약대로 `ROLLBACK` 성공은 `ROLLED_BACK_HELD`, `RESUME(expectedLocation: SOURCE)`
  성공은 `RESTORED`를 반환합니다. 컨트롤플레인은 그 후 앱을 재개하고 양쪽 환경의 업무 검사를 수행합니다.
  응답 유실과 UNKNOWN은 기존 조회·RECONCILE 경로로 복구합니다.
- 콘솔의 리소스 응답에 에이전트의 `rollback`, `details`를 그대로 추가 전달합니다.
  역방향 CDC 상세 필드의 이름은 에이전트 계약을 따릅니다. 컨트롤플레인은 이 필드로 안전성을 추측하지 않고
  `readiness.rollback`을 사용합니다. DB의 데이터 유실 허용 `failure` 모드는 추가하지 않습니다.

이 연동은 컨트롤플레인의 정책·순서·복구 검사로 검증합니다. 새 DB 에이전트와 실제 역방향 CDC의
통합 검증은 해당 에이전트 구현을 연결한 뒤 별도로 수행해야 합니다.

## 설정

`config/seed.example.yaml`의 DB 설정을 사용합니다. `profile`을 생략해도 직접 논리 복제를 선택합니다. `replicationRef`와 `catalog.dmsJob`은 넣지 않습니다. 두 DB에는 같은 schema를 준비하고, 실제 Secret ARN과 DB 주소를 등록합니다. 현재 DB 에이전트의 관리형 프로필은 RDS PostgreSQL 17 → 독립 Cloud SQL PostgreSQL 17을 지원합니다.

오케스트레이터에는 DB SDK가 필요합니다. DB·업무 범위와 정지 근거를 SDK로 게시하고, 실행 명령은 Redis와 같은 `/v1` API로 보냅니다. SDK는 DB 에이전트와 같은 버전을 사용합니다. v2를 포함한 두 저장소의 현재 코드를 함께 검증합니다. 기존 `db-migration-adaptor@68a0c10`은 v1이므로 v2 계획을 지원하지 않습니다. 지원 기능 검사에서 이 차이를 확인합니다.

```sh
uv sync --locked
uv pip install -e ../db-migration-adaptor
uv run --no-sync control-plane
```

Docker 빌드는 기존 `DB_OPERATOR_REF` 인자에 승인한 DB SDK 커밋을 지정하고, `github_token` 빌드 Secret을 전달합니다. SDK가 없는 이미지에서는 DB 작업을 진행할 수 없습니다.

기존 DMS 설정을 유지할 때는 `profile: gcp-dms-postgres-pgbouncer-v1`을 명시하고 `replicationRef`와 `catalog.dmsJob`을 함께 넣습니다. 직접 논리 복제 설정에 DMS 항목이 남아 있으면 시작을 거절합니다.

기존 직접 논리 복제 동작은 `profile: postgres-managed-logical-pgbouncer-v1`로 선택합니다. v1과 DMS는 기존의 정지·경로 변경·재개 분리 동작을 유지합니다.

## 완료 조건

- 기본 설정으로 DMS 참조 없이 계획을 생성합니다.
- DMS 전용 기능만 제공하는 서버에는 직접 논리 복제 계획을 보내지 않습니다.
- 실제 DB SDK로 DB·업무 범위를 등록하며 DMS 작업을 등록하지 않습니다.
- 앱 정지 확인, 명령 재시도, 응답 유실 복구 검사를 통과합니다.
- PostgreSQL·PgBouncer를 사용하는 별도 로컬 랩에서 복사와 전환을 확인합니다.

## 2026-11-06 검증

- 오케스트레이터: 186개 통과, 외부 Redis·오브젝트·트래픽 서버가 필요한 5개는 미실행입니다. Ruff, mypy와 패키지 빌드도 통과했습니다.
- 실제 DB SDK로 DB 두 개와 업무 범위를 등록했습니다. 이 검사는 moto 저장소를 사용하며 DMS 작업 등록 함수를 호출하면 실패하도록 했습니다.
- 오케스트레이터의 실제 프로필·HTTP 클라이언트·정지 근거 SDK를 DB 에이전트에 연결했습니다. PostgreSQL 17 두 개, PgBouncer, DynamoDB Local에서 복사 → 정지 → 경로 변경 → 쓰기 재개 검사가 통과했습니다.
- 같은 키의 재전송, CUTOVER 직후의 대상 주문, sequence 증가, 원본 writer 차단과 RESUME의 읽기 확인을 검증했습니다. DMS 클라이언트를 만들면 검사가 실패합니다.

DB API 연결 검사는 ASGI 방식으로 실행했습니다. Cloud SQL 인스턴스 메타데이터 응답만 모의입니다. 실제 ECS 배포와 클라우드 간 네트워크를 재검증한 결과는 아닙니다. 실행 중인 클라우드 시연은 변경하지 않았습니다.
