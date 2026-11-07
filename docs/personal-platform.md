# 개인 workspace 플랫폼

사용자 앱은 이미 만든 Docker 이미지를 사용합니다. 플랫폼은 앱과 리소스를 등록하고 마이그레이션·승인·복구를 제어합니다. 빌드·CI/CD·Webhook·팀 workspace는 구현하지 않습니다.

## 로컬 실행

Docker와 uv가 필요합니다. 저장소 루트에서 실행합니다.

```bash
make setup
make local-run
```

- API: http://127.0.0.1:8000
- API 문서: http://127.0.0.1:8000/docs
- DynamoDB Local: http://127.0.0.1:18005
- 로컬 계정: `local:developer`. 로그인을 생략하며 API는 loopback에만 바인딩합니다.
- 상태는 `foxtrot-platform-dynamodb` 볼륨에 보존합니다. API 재시작 후에도 앱과 실행 기록이 남습니다.

`dev/run_local.py`가 로컬 DB 컨테이너와 테이블을 준비합니다. AWS·GCP 자원은 만들지 않습니다. API를 끝내려면 해당 터미널에서 Ctrl+C를 누릅니다. DB도 멈추려면 `docker stop foxtrot-platform-dynamodb`를 실행합니다. 볼륨은 삭제하지 않습니다.

생성한 `runtime/seed.yaml`의 리소스 값은 설정 예제입니다. 실제 이전 서버·업무 앱이 연결된 상태를 뜻하지 않습니다. 연결이 없으면 검증과 실행 요청이 차단됩니다. 기존 프론트엔드 데모는 이 신규 API와 아직 통합하지 않았습니다.

## 등록과 실행 순서

모든 변경 요청에는 `Idempotency-Key`를 보냅니다. 실제 로그인 환경에서는 세션 쿠키·Origin·CSRF 토큰도 필요합니다. 소유자는 요청 본문이 아닌 세션에서 결정합니다.

1. `POST /v1/applications`: 앱 이름과 `image`를 저장합니다. `imageRef`는 `@sha256:` digest로 고정합니다.
2. `GET /v1/catalog`: 본인 workspace에 할당한 환경·리소스 목록을 조회합니다.
3. `PUT /v1/applications/{id}/resources`: 본인에게 승인한 서버 카탈로그를 연결합니다. 본문에 `expectedVersion`을 보냅니다.
4. 필요하면 `PUT /v1/applications/{id}/policy`로 정책을 저장합니다. 설정을 바꾸면 재검증이 필요합니다.
5. `POST /v1/applications/{id}/environments/{env}/prepare`: 승인된 실행기에 대상 앱 기동을 요청합니다. `202`와 runtime 작업을 반환합니다.
6. `GET /v1/runtime-operations/{id}`로 `READY`를 확인합니다. 이는 컨테이너·서비스 상태 확인이며 업무 검증과는 다릅니다.
7. `POST /v1/applications/{id}/verify`: 이미지 실행 근거, 앱 `/readyz`, 어댑터 프로필, 모든 업무 참여자의 최신 RUNNING 응답을 검사합니다. 최신 앱 `expectedVersion`을 보냅니다.
8. `POST /v1/workflows`: `applicationId`, `sourceEnv`, `targetEnv`를 보냅니다. 검증된 앱의 이미지·설정·정책을 고정해 저장합니다.
9. 기존 `/v1/workflows/{id}/commands`로 승인·중단·복구를 제어합니다. 본문의 `expectedVersion`은 workflow 버전입니다.

앱 수정·해제의 버전은 `Expected-Version` 헤더에 보냅니다. 등록·리소스 연결·정책·검증·runtime 작업의 입력과 출력은 OpenAPI에서 확인합니다.

```json
{
  "name": "goods",
  "image": {
    "imageRef": "registry.example.com/goods@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "port": 8000,
    "cpu": 256,
    "memory": 512,
    "architecture": "ARM64",
    "environment": {"APP_ENV": "production"},
    "secretRefs": {"DATABASE_URL": "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:goods"}
  }
}
```

`GET /v1/applications/{id}`에서 최신 앱 버전을 확인합니다. 등록은 앱 실행을 뜻하지 않습니다. 앱 해제는 플랫폼 등록을 보관 처리하며 DB·파일·배포 자원을 삭제하지 않습니다. 같은 업무 scope를 새 앱으로 재할당하려면 서버 관리자가 기존 연결을 정리해야 합니다.

## 서버가 승인할 설정

- `CONTROL_PLANE_SEED_OWNER_ID`: 기본 카탈로그의 GitHub 숫자 ID 기반 소유자. 예: `github:12345`.
- `CONTROL_PLANE_PERSONAL_CATALOG_PATHS`: 사용자 ID → 서버 YAML 파일 경로. 사용자가 파일 경로나 DB 관리 자격을 API로 지정하지 않습니다.
- `CONTROL_PLANE_PERSONAL_SECRET_REFS`: 사용자 ID → 허용할 Secret 참조 목록. 다른 사용자의 참조를 고를 수 없습니다.
- `CONTROL_PLANE_WORKLOAD_TOKEN_SECRETS`: 업무 scope → Secret 참조. 다른 scope의 토큰으로 보고할 수 없습니다. 기본 seed scope는 기존 workload 토큰을 유지합니다.
- `CONTROL_PLANE_RUNTIME_TARGETS`: `사용자ID/환경ID` → 서버 승인 실행 설정.

카탈로그는 `sourceEnvironment`와 `targetEnvironment`를 지정합니다. 리소스 참조도 그 방향에 맞춰 등록합니다. 이전 YAML 호환용으로만 환경 선언 순서에서 방향을 읽는 경로를 유지합니다. 환경 이름만 바꿔 역방향 이전을 요청하면 거절합니다.

AWS 실행 설정 예시:

```json
{
  "github:12345/aws": {
    "provider": "ecs",
    "cluster": "approved-cluster",
    "service": "approved-service",
    "taskDefinition": "approved-template:1",
    "container": "app",
    "region": "ap-northeast-2"
  }
}
```

ECS 실행기는 승인된 task definition을 복제하고 이미지·자원·앱 설정을 반영한 뒤 지정된 service를 갱신합니다. 기존 환경 변수와 Secret 설정은 유지하고 같은 이름의 앱 설정을 덮어씁니다. 실행 역할·네트워크·service는 사용자가 지정할 수 없습니다. 기존 ECS rolling service와 양수 desired count가 필요합니다. service와 연결된 포트도 일치해야 합니다.

이미 실행 중인 GCP·온프레미스 앱은 인증된 heartbeat에 고정 `imageRef`를 포함하면 연결할 수 있습니다. 대상 환경의 모든 참여자·예상 프로세스 수·최신 generation·보고 시각·이미지가 일치해야 합니다. 이는 관리 앱이 보고한 이미지 근거이며 클라우드 API로 컨테이너를 검사한 결과는 아닙니다.

GCP·온프레미스 자동 기동 실행기는 제공하지 않습니다. 준비된 환경 연결과 리소스 이전 제어를 자동 앱 기동 지원으로 표시하지 않습니다. [ECS service 갱신 문서](https://docs.aws.amazon.com/boto3/latest/reference/services/ecs/client/update_service.html)

## 실패와 복구

- 앱 수정, runtime 준비, 마이그레이션은 DynamoDB 조건부 트랜잭션으로 충돌을 막습니다. 현재 변경 실행은 전역 한 개입니다.
- Runtime은 `PENDING → APPLYING → VERIFYING → READY`로 진행합니다. 시작 의도를 먼저 저장합니다.
- 응답 유실이나 APPLYING 상태의 프로세스 중단은 `UNKNOWN`으로 멈춥니다. 자동 재기동하지 않습니다.
- `POST /v1/runtime-operations/{id}/reconcile`은 관측만 수행합니다. Docker label 또는 ECS task definition의 작업 tag로 같은 기동을 확인합니다.
- 기존 service가 보인다는 이유만으로 UNKNOWN을 해제하지 않습니다. 실제 상태를 입증할 수 없으면 잠금을 유지하고 운영자가 대상과 실행 기록을 대조해야 합니다.
- `POST /v1/runtime-operations/{id}/restore`는 새 데이터 쓰기가 시작되지 않은 경우에만 허용합니다. ECS는 이전 task definition을 복원하고 배포 완료를 기다립니다. 로컬 Docker는 해당 작업이 만든 컨테이너만 제거합니다.
- 데이터 롤백은 기존 리소스 명령으로 따로 수행합니다. DB의 대상 새 쓰기 후 복귀와 전체 자동 롤백은 지원하지 않습니다.
- 요청 처리 중 예상하지 못한 오류가 나면 멱등 키를 자동 해제하지 않습니다. 접수 여부가 불명인 작업을 새 키로 재실행하지 말고 저장 기록을 먼저 확인합니다.

정책 변경·거절은 앱 감사 이력에서 확인합니다. 승인과 리소스 명령은 workflow 이력에서 확인합니다. 앱 저장과 기본 감사 기록은 같은 트랜잭션에 포함합니다.

## 검증 범위

`make check`는 정적 검사·타입 검사·단위 검사·조건부 통합 검사를 실행합니다.

```bash
make local-test
```

이 검사는 실제 DynamoDB Local·Docker·NGINX 이미지와 HTTP API를 사용합니다. 등록 멱등성, 앱 소유 관계, 컨테이너 기동·HTTP 응답·복귀, 정책 거절, 실행 중 변경 차단, 설정 고정, 엔진 재시작과 전체 단계 완료를 확인합니다.

리소스 이전 서버·업무 참여자·업무 검사는 모의 구현을 사용합니다. 따라서 NGINX가 업무 정지를 지원하거나 실제 DB·Redis·파일이 이전됐다는 증거는 아닙니다. ECS 호출은 모의 클라이언트 검사이며 실제 AWS 배포는 수행하지 않았습니다. 최신 실행 결과는 `docs/local-validation.md`에 기록합니다.
