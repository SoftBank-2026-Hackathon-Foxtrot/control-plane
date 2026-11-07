# 앱이 구현할 내용

DB 전환에는 앱과 Worker의 실제 업무 정지가 필요합니다. 앱은 플랫폼에 주기적으로 상태를 보고하고 지시를 받습니다. 앱 VM에 인바운드 경로가 없고, 전환 대상인 DB와 Redis에 의존하면 안 되므로 앱이 플랫폼으로 연결합니다.

## Heartbeat

```http
POST http://control-plane.internal:8000/internal/v1/workloads/scope:goods/heartbeat
Authorization: Bearer <WORKLOAD_TOKEN>
Content-Type: application/json
```

```json
{
  "actorId": "goods-aws-api",
  "instanceId": "ip-10-10-0-10-1234",
  "generation": 17,
  "state": "RUNNING",
  "inFlight": 3
}
```

```json
{
  "desired": "RUN",
  "generation": 17,
  "objectMigrationId": "mig-1234",
  "pollAfterMs": 5000
}
```

| 필드 | 의미 |
| --- | --- |
| `actorId` | 시드의 `actors`에 등록한 이름. 환경과 역할별로 하나 |
| `instanceId` | 프로세스마다 다른 값. `hostname-pid`를 권장 |
| `generation` | 응답에서 마지막으로 받은 값. 처음에는 0 |
| `state` | `RUNNING`, `HOLDING`, `HELD` |
| `inFlight` | 게이트를 통과해 처리 중인 작업 수 |

uvicorn Worker를 포함한 **모든 프로세스가 각자** 보고합니다. 플랫폼은 시드에 적은 프로세스 수만큼 보고가 모여야 해당 actor를 확인된 것으로 봅니다. 15초 동안 보고가 없는 프로세스는 빠집니다.

## 프로세스 동작

| 응답 | API 프로세스 | Worker |
| --- | --- | --- |
| `desired=HOLD` | 새 요청과 WebSocket 처리를 게이트에서 기다리게 합니다. 처리 중인 요청이 끝날 때까지 `HOLDING`, 0이 되면 `HELD`를 보고합니다 | 새 메시지를 가져가지 않습니다. 처리 중인 작업을 끝내면 `HELD`를 보고합니다 |
| `desired=RUN` | 게이트를 열고 `RUNNING`을 보고합니다 | 작업을 재개하고 `RUNNING`을 보고합니다 |

- **`/readyz`:** 게이트를 거치지 않습니다. 전환 중 정상 환경이 Cloudflare Pool에서 빠지면 안 됩니다.
- **대기 상한:** 게이트 대기는 60초까지입니다. 넘으면 `503`과 `Retry-After`를 반환합니다. Cloudflare의 100초 제한보다 먼저 응답하기 위한 값입니다.
- **시작:** 첫 응답을 받기 전까지 `/readyz`를 503으로 두고 업무를 받지 않습니다. 재시작한 프로세스가 진행 중인 정지를 놓치면 안 됩니다.
- **통신 단절:** 마지막 지시를 유지합니다. HOLD는 스스로 풀지 않습니다.

## 오브젝트 경로

응답의 `objectMigrationId`가 있으면 `OBJECT_MIGRATION_ID_FILE`에 기록합니다. `objroute.py`가 이 파일을 다시 읽어 경로 조회를 시작합니다. 앱에는 `OBJECT_MIGRATION_SERVER`와 `OBJECT_MIGRATION_ID_FILE`을 설정합니다.

## Redis 경로

시드의 `resources.redis.directWhenIdle`이 `true`이면, 앱은 평소에 Redis에 직접 연결하고 이전하는 동안만 Redis 프록시를 거칩니다(Redis 설계 3-8). 플랫폼은 PREPARE 전에 모든 프로세스를 프록시로 옮기고, FINALIZE나 ABORT 뒤에 Redis가 남은 쪽으로 직접 연결하게 합니다. 어느 쪽으로 옮기든 두 경로가 같은 Redis를 가리키므로 데이터는 바뀌지 않습니다.

응답에 경로 지시가 함께 옵니다.

```json
{
  "redisRoute": "PROXY",
  "redisEndpoint": null,
  "redisRouteGeneration": 3
}
```

| 응답 필드 | 의미 |
| --- | --- |
| `redisRoute` | `DIRECT` 또는 `PROXY` |
| `redisEndpoint` | `DIRECT`일 때 연결할 환경 이름(`aws`, `gcp`). `null`이면 앱의 기본값(`REDIS_URL`) |
| `redisRouteGeneration` | 경로 지시의 번호. 지시가 바뀔 때마다 1씩 커집니다 |

보고에 지금 쓰는 경로를 넣습니다. 이 기능을 쓰지 않는 프로세스는 넣지 않습니다.

```json
{
  "redisRoute": "PROXY",
  "redisRouteGeneration": 3,
  "staleConnections": 0
}
```

| 보고 필드 | 의미 |
| --- | --- |
| `redisRoute` | 지금 새 명령을 보내는 경로 |
| `redisRouteGeneration` | 따른 경로 지시의 번호 |
| `staleConnections` | 떠난 경로에 아직 열려 있는 연결 수. 옛 연결에서 진행 중인 명령, 아직 옮기지 않은 구독을 셉니다 |

- **옮기는 방법:** 새 명령은 바로 새 경로의 연결 풀로 보내고, 옛 풀은 쓰는 중인 연결이 없어지면 닫습니다. Pub/Sub은 새 경로에 먼저 구독하고 확인을 받은 뒤 옛 구독을 해제합니다. 겹치는 동안 두 번 받은 메시지는 메시지 ID로 버립니다. 큐 소비자는 같은 consumer 이름으로 새 경로에서 다시 읽습니다.
- **연결 이름:** 직접 연결에는 `CLIENT SETNAME direct:<actorId>`를 붙입니다. 프록시를 거치는 연결에는 붙이지 않습니다. 프록시가 이름을 자기 업스트림에 넘기기 때문입니다.
- **확인:** 등록된 모든 프로세스가 같은 `redisRouteGeneration`을 보고하고 `staleConnections`가 0인 상태가 `redisRouteStableWindow`(기본 10초) 이어져야 진입으로 봅니다. 그 뒤 Redis 이전 서버의 PREPARE가 원본의 `CLIENT LIST`에서 직접 연결이 0개인지 한 번 더 확인합니다.
- **시간 초과:** `redisRouteTimeout`(기본 120초) 안에 다 옮기지 않으면 `DIRECT`로 되돌리고 `REDIS_ENTRY_TIMEOUT`으로 멈춥니다.
- **시작:** 첫 응답의 경로를 적용한 뒤에 업무를 받습니다. 이전 중에 뜬 프로세스도 처음부터 프록시로 붙습니다.

앱 설정은 `REDIS_URL`(기본값, 평소의 직접 연결), `REDIS_PROXY_URL`(프록시와 그 프로세스의 프록시 계정), `REDIS_DIRECT_URLS`(`aws=redis://...,gcp=redis://...`)입니다. `REDIS_DIRECT_URLS`가 없으면 `REDIS_URL`만 씁니다.

## 업무 검사 지원

플랫폼은 환경별로 다음을 호출합니다.

```txt
POST /api/login/probe-platform
POST /api/groupbuys/{groupbuyId}/join?qty=1
GET  /api/me/requests
GET  /api/me/receipts/{requestId}
POST {cleanupPath}
POST /api/logout
```

- `probe.groupbuyId`의 상품은 검사 전용으로 두고 재고를 충분히 넣습니다. `SOLD_OUT`은 검사 실패로 처리합니다.
- `probe.cleanupPath`는 시험 주문을 정리하는 내부 경로입니다. 본문은 `{"requestId": "..."}`이고 `X-Session` 헤더로 검사 계정의 세션을 보냅니다. 앱은 검사 계정의 주문만 정리해야 합니다. 설정하지 않으면 시험 데이터가 남습니다.
- 사용자당 요청 제한(`RL_LIMIT`)에 걸리지 않도록 검사 계정은 제외하거나 충분한 값을 둡니다.
