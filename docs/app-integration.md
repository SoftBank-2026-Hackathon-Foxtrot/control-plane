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


## 이미 실행 중인 관리 앱의 이미지 보고

개인 workspace에 고정 이미지로 앱을 등록한 경우 heartbeat에 `imageRef`를 포함합니다. 모든 API·Worker 프로세스가 실제 배포한 digest를 보고해야 합니다. 대상의 예상 프로세스 수, generation, 보고 만료 시간도 함께 검사합니다.

플랫폼의 ECS·Docker 실행기는 `FOXTROT_IMAGE_REF` 환경 변수에 고정 이미지 주소를 주입합니다. 이미 실행 중인 환경은 관리자가 같은 값을 설정해야 합니다. 기존 heartbeat는 `imageRef` 없이도 업무 제어에 사용할 수 있지만, 이미지 실행 근거로는 사용할 수 없습니다.

추가 workload scope는 `CONTROL_PLANE_WORKLOAD_TOKEN_SECRETS`에 별도 토큰을 할당합니다. 토큰은 브라우저에 전달하지 않습니다. 이미지 보고는 관리 프로세스의 선언이며 provider가 증명한 이미지 검사와는 구분합니다.
