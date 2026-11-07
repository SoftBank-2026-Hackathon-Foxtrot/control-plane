# 마이그레이션 관측 연결

모듈의 기존 상태 API → 컨트롤 플레인 저장 → 플랫폼 상세 화면 순서입니다. 모듈은 서비스를 재배포하면 추가 필드를 제공합니다. 화면은 어댑터나 클라우드 API에 직접 연결하지 않습니다. CPU·메모리·전체 로그 시스템은 추가하지 않습니다.

## 수집과 조회

- `ObservationLoop`가 등록된 DB·Redis·객체 migration을 5초마다 조회합니다. 승인 대기·중단·복귀 중에도 동작하고, 종결 후 60초까지 계속합니다. 엔진과 동시에 같은 migration을 읽으면 HTTP 요청을 공유합니다. 제어 엔진의 tick 주기는 유지합니다.
- 관측은 `OBS_LATEST#resource`와 시간순 `OBS#...`에 저장합니다. 엔진의 `RESOURCE#...`를 수정하지 않습니다. 원 측정 시각, 수집 시각, 마지막 성공, 마지막 진전을 구분합니다. 수집 실패 시 마지막 성공값을 유지하고 `UNAVAILABLE`로 표시합니다.
- 최신성 기준은 15초입니다. 원 시각이 없거나 미래이면 `UNKNOWN`입니다. `checks[].decision`은 당시 결과이고 `currentResult`는 시각·전환·topology가 유효할 때만 해당 결과입니다. 화면용 계산이며 제어 판정을 바꾸지 않습니다.
- `GET /v1/workflows/{id}`는 progress/checks/rollback/details, 준비 결과, 공용·업무 probe, 실제 명령 ID와 시간을 제공합니다. 기존 `verified`/`effectivePercent`는 **LB 설정 확인**입니다. 새 화면은 `configVerified`와 `publicProbe.observedPercent`를 구분합니다.
- `GET /v1/workflows/{id}/observations?since=<ISO8601>&limit=200&cursor=...`는 로그인 인증을 사용합니다. 최대 500개, 최근 24시간 이내이며 다음 페이지는 `nextCursor`를 사용합니다. 시간 조건을 DynamoDB key query에 적용합니다.
- 원시 표본의 `expiresAt`은 24시간, 종결 후 최신 관측/공용 요약은 7일입니다. 활성 workflow의 최신 관측과 제어 근거는 만료시키지 않습니다. DynamoDB TTL은 아래처럼 **운영 테이블에 별도로 활성화**해야 삭제됩니다. API는 이력의 24시간 범위를 제한합니다. 메모리 저장소는 개발용이며 재시작 시 사라집니다.

```sh
aws dynamodb update-time-to-live --table-name "$CONTROL_PLANE_TABLE_NAME" \
  --time-to-live-specification 'Enabled=true,AttributeName=expiresAt'
```

## 설정

`.env.example`의 선택 설정을 채웁니다. 실제 토큰은 환경 주입 또는 Secrets Manager 참조를 사용합니다.

```dotenv
CONTROL_PLANE_PUBLIC_PROBE_URL=https://app.example.com/api/migration-probe
CONTROL_PLANE_PUBLIC_PROBE_SERVED_BY={"aws-web":"aws","gcp-web":"gcp"}
CONTROL_PLANE_OBSERVATION_TOKEN_SECRET=env:OBSERVATION_TOKEN
```

`PUBLIC_PROBE_URL`은 Cloudflare를 통하는 **앱 URL**입니다. OAuth callback용 `CONTROL_PLANE_PUBLIC_URL`과 다릅니다. `SERVED_BY` 매핑의 값은 seed의 환경 이름과 일치해야 합니다. demo 앱의 `SERVED_BY` 값도 등록합니다. URL을 생략하면 공용 probe를 보내지 않습니다. 준비 토큰을 생략하면 수입 경로는 비활성입니다.

공용 probe는 1초 간격을 목표로, 한 번에 1개 요청과 2초 timeout을 사용합니다. HTTP 200 + `{"ok":true}`를 확인합니다. `Cache-Control: no-store`, Cloudflare cache 상태와 `Age`를 검사하며 캐시/위치 불명 응답은 도착 비율에서 제외합니다. 60초 구간의 시도·실패·timeout·위치 미확인·누락·최대 간격과 성공 p95를 함께 표시합니다. timeout은 실패에도 포함됩니다. 30개 표본 기준은 표시 참고값이며 canary의 통계적 안전성을 보장하지 않습니다. 실패 구간도 샘플 시각이며 전체 사용자 중단시간이 아닙니다.

## 준비 작업 연결

`POST /internal/workflows/{id}/preparation`은 별도 `OBSERVATION_TOKEN`으로 인증합니다. workload heartbeat 토큰과 다릅니다. `resource`는 `INFRA` 또는 `COMPUTE`, `data.workflowId`는 URL의 실제 workflow ID여야 합니다. 원래 `observedAt`과 허용된 요약 필드만 저장합니다. 서비스 URL이나 실행 명령을 수입하지 않습니다.

IaC 저장소의 `tools/report_observations.py`가 실제 job/plan/handoff 또는 COMPUTE 상태 API 결과를 이 경로로 보냅니다. `--watch`는 COMPUTE를 5초마다 읽는 실행 모드입니다. Terraform plan/apply 실행기 자체를 구현하지는 않습니다. COMPUTE는 PREPARE/START_SYNC 준비 프로필이며 데이터 모듈의 CUTOVER/ROLLBACK 순회에 추가하지 않습니다.

## 적용 순서와 완료 확인

1. 각 모듈과 demo의 같은 이름 브랜치를 검토·배포합니다. DB `details.database`, Redis `details.redis.sync/proxy`, 객체의 시각/앱 ack/검증 필드를 확인합니다.
2. 컨트롤 플레인의 seed에 실제 어댑터 주소·secret 참조·기대 앱 수를 등록합니다. 관측 설정과 DynamoDB TTL을 적용합니다.
3. workflow ID에 맞춰 실제 IaC·COMPUTE 준비 결과를 보고합니다. 예제 handoff를 실행 근거로 사용하지 않습니다.
4. 플랫폼의 `VITE_API_BASE_URL` 또는 `/api` 프록시를 컨트롤 플레인에 연결합니다. 서비스 토큰을 `VITE_*`에 넣지 않습니다.
5. 리허설에서 정상 이전·허용된 복귀·수집 단절·WebSocket 잔존을 확인합니다. 캐시된 모듈 응답과 통신 실패가 최신 정상값으로 보이지 않는지 확인합니다.

DB의 기본 관리형 논리 복제 프로필은 대상 쓰기 후 역복제를 제공하지 않습니다. `targetWritesPossible`은 실제 쓰기 수가 아닙니다. Redis `copied`는 재복사 포함 처리 건수, `lag_ms`는 heartbeat/알림 계열 값입니다. 객체 앱 `live`와 현재 generation의 `acked`를 구분하고 기대 앱 수는 seed에서 유지합니다. 동기화/검증 시각이 없는 초기 backlog와 복사량은 미확인으로 표시합니다.

앱의 HTTP 업무 in-flight와 WebSocket 잔존 수는 별도입니다. 미보고·만료된 인스턴스가 있으면 WebSocket 합계는 null입니다. 새 workflow가 시작되면 이전 workflow에는 저장된 앱 관측을 표시합니다.

이 변경의 자동 테스트는 fake/임시 저장소와 로컬 어댑터를 사용합니다. 실서비스 배포, 실제 데이터 이전, Cloudflare 설정 변경은 별도 리허설에서 검증해야 합니다.
