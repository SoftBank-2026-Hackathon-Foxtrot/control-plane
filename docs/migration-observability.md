# OTEL 마이그레이션 관측

`모듈 SDK → OTEL Collector → CP / Prometheus / Tempo → CP API → 플랫폼`으로 통일합니다. 별도 관측용 adapter polling과 IaC preparation POST를 제거했습니다. 엔진이 명령 결과와 안전 근거를 확인하는 기존 제어 API 호출은 유지합니다.

## 최소 수집 항목과 위치

| 발행 위치 | 필요한 정보 |
|---|---|
| DB API background observer / Worker | 복제 상태·lag·검증·rollback 근거, PgBouncer pause, writer/prepared 수, 작업 시작·종료 |
| Redis API observer / CommonApi 실행 | 정·역방향 backlog/copied/dirty age, proxy in-flight/open transaction/pause/pubsub, 실제 명령 span |
| Object Go observer / worker | 복사 수·bytes, 정·역복제, 마지막 sync/verification, 앱 generation/ack, 명령 span |
| IaC Provisioner / COMPUTE adapter | plan/apply 변경 수·작업 상태·실제 시각, VM/이미지/설정/컨테이너·readiness |
| Traffic 실행기 | 요청/적용 LB weight, 설정 검증, 작업 상태·시간 |
| Demo 앱 / worker | 요청·업무 span, in-flight, WebSocket 잔존, Redis 경로의 잔존 연결 |
| CP coordinator / public probe | workflow에 연결한 HOLD/actor 요약, 실제 공용 요청 실패·timeout·p95·도착 위치 |

Redis Go proxy/syncworker와 PgBouncer의 관리 카운터는 소유 모듈의 SDK가 읽어 발행합니다. 프록시의 기존 프로토콜과 제어 경로는 바꾸지 않습니다. 앱 heartbeat 역시 제어 프로토콜로 유지하고 그 요약을 CP SDK에서 OTEL로 보냅니다.

## 수신·조회 계약

- `POST /internal/otlp/v1/logs`: 표준 OTLP HTTP/protobuf. Collector 전용 Bearer 인증, 1 MiB/512 records 제한, `compression: none`. 계약 버전·발행 module·등록 workflow/migration·원 시각을 검사하고 다시 allowlist를 적용합니다. 잘못된 표본은 OTLP partial success로 거부하고, 저장소 장애는 503으로 재시도를 요청합니다.
- `GET /v1/workflows/{id}`와 `/observations`는 기존 UI 모델을 유지합니다. snapshot source는 `OTEL`입니다. 엔진 `RESOURCE#`와 관측 `OBS_LATEST#`는 별도이며 오래된 전달은 최신값을 덮어쓰지 않습니다. 수집 실패는 마지막 성공값을 보존합니다.
- `/telemetry`는 인증된 고정 PromQL/TraceQL 조회입니다. 16개 핵심 수치와 최근 10개 trace만 반환하며, 임의 URL/쿼리를 받지 않습니다. backend 미설정·단절·빈 결과를 구별합니다. 완료된 workflow는 완료 시각 기준으로 조회합니다.
- 원본 시각이 15초를 넘으면 `STALE`, 없거나 미래이면 `UNKNOWN`, 명시적 관측 실패는 `UNAVAILABLE`입니다. metric 표본 시각과 `foxtrot.observation.age`를 함께 봅니다. 과거 PASS를 현재 전환의 허가로 재사용하지 않습니다.
- snapshot 이력 24시간, 완료 workflow 최신 snapshot 7일 TTL. 활성 workflow 최신 상태는 유지합니다. 운영 DynamoDB의 `expiresAt` TTL은 IaC platform root에서 활성화합니다. Prometheus/Tempo 예제 보존 기간은 24시간입니다.
- 이전 `/internal/workflows/{id}/preparation`은 410 `USE_OTLP`입니다. IaC 보고 CLI도 OTEL SDK를 사용합니다.

## CP 환경

```dotenv
CONTROL_PLANE_OBSERVATION_TOKEN_SECRET=env:OBSERVATION_TOKEN
CONTROL_PLANE_PROMETHEUS_URL=http://prometheus.internal:9090
CONTROL_PLANE_TEMPO_URL=http://tempo.internal:3200
CONTROL_PLANE_PUBLIC_PROBE_URL=https://app.example.com/api/migration-probe
CONTROL_PLANE_PUBLIC_PROBE_SERVED_BY={"aws-web":"aws","gcp-web":"gcp"}
```

CP도 아래 공통 SDK 환경을 설정해야 APP/PUBLIC 표본과 작업 trace를 발행합니다. 공용 URL은 OAuth callback URL이 아닌 Cloudflare **앱 ingress**입니다. 무캐시 200 + `{"ok":true}`/`X-Served-By`를 검사합니다. 60초 probe 비율은 실제 전체 사용자 트래픽 비율이 아니며, probe 실패 구간은 전체 사용자 중단시간이 아닙니다. 미보고 WebSocket 수는 null입니다. LB `configVerified`와 공용 도착 위치를 구분합니다.

## 검증

`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`.
`tests/integration/test_otel_stack.py`는 SDK → 실제 Collector → CP/Prometheus/Tempo 및 stale 처리를 검증합니다. 파일 머리의 격리 포트/테스트 토큰으로 로컬 stack을 실행한 뒤 `RUN_OTEL_STACK=1 uv run pytest tests/integration/test_otel_stack.py`로 재현합니다. 클라우드 이전과 Cloudflare 변경은 실행하지 않습니다.

## OTEL 실행 설정

공식 OpenTelemetry SDK가 OTLP HTTP/protobuf로 로그·메트릭·트레이스를 내보냅니다. 아래 값을 **프로세스 환경**에 주입합니다. endpoint가 없거나 `OTEL_SDK_DISABLED=true`이면 전송하지 않습니다. `.env` 파일을 만드는 것만으로 Python 프로세스 환경이 바뀌지는 않습니다.

```dotenv
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.internal:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<module-ingest-token>
```

Collector는 IaC의 `observability/compose.yaml`로 준비합니다. Collector → CP에는 별도 `OBSERVATION_TOKEN`을 씁니다. 모듈에 CP 수신 토큰을 주지 않습니다. 운영 주소는 사설망/VPN에서 접근하도록 구성하고, TLS를 사용하는 경우 SDK의 `OTEL_EXPORTER_OTLP_CERTIFICATE`와 HTTPS endpoint를 설정합니다. 앱 환경 값·DSN·Terraform output·원시 키·오류 원문은 전송하지 않습니다.

진행 상태는 OTEL 구조화 로그 → Collector → CP의 `OBS_LATEST`/24시간 이력, 수치는 Prometheus, 작업 span은 Tempo에 저장합니다. 플랫폼은 CP API만 조회합니다. 제어 명령·승인·멱등성·판정 근거는 기존 저장소가 원본이며 OTEL 전송 성공을 전환 조건으로 사용하지 않습니다. 관측이 끊겨도 이전 값을 0이나 성공으로 바꾸지 않습니다.

Python `telemetry.py`/`telemetry_contract.py`는 독립 이미지에 포함한 공통 계약 v1 사본입니다. 계약을 바꾸면 CP와 모든 Python 발행자의 사본 및 SDK 계약 테스트를 함께 갱신합니다. metric에는 operation/transition ID·키·URL을 label로 넣지 않으며, 값 캐시는 최대 2048 series/20초입니다. workflow ID는 마이그레이션별 화면 조회를 위한 제한된 차원입니다.
