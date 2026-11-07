# AWS → GCP 보안 비교 데모

선택한 AWS 앱 정책과 GCP 버킷의 직접 부여 권한을 비교한다. 앱의 기존 접근은 유지하고, 별도 빈 버킷에 추가한 접근 권한만 제거한다. 검사 결과와 작업 기록은 컨트롤 플레인 테이블에 저장한다.

## 검사 범위

1. AWS 앱 역할의 `workload-bucket` 인라인 정책이 앱 버킷의 목록·위치 조회와 객체 읽기·쓰기·삭제만 허용하는지 확인한다.
2. GCP 앱 계정이 앱 버킷의 무조건부 `roles/storage.objectUser` 권한을 유지하는지 확인한다.
3. 별도 데모 버킷에 같은 계정의 직접 부여 권한이 있는지 확인하고 제거한다.
4. 재검사에서 위 조건과 GCP 앱 `/readyz` HTTP 200을 확인한다.

AWS/GCP의 역할 전체가 동등하다는 판정은 아니다. AWS의 다른 인라인·관리형 정책, GCP 프로젝트/조직 상속 권한, 그룹, ACL, deny 정책, 조건부 정책의 실효 권한, DB·Redis, 객체 CRUD의 실제 성공/거부는 검사하지 않는다. 지원하지 않는 정책 형태는 `unknown`으로 표시하고 변경안을 만들지 않는다. `/readyz`는 앱 준비 응답이며 객체 접근 테스트를 대체하지 않는다.

## API와 적용 순서

| 요청 | 동작 |
| --- | --- |
| `GET /v1/security-review` | 저장된 상태, 검사 근거, 서버 기록 조회 |
| `POST /v1/security-review/scan` | IAM/GCS 설정과 앱 상태 조회 |
| `POST /v1/security-review/plan` | 제거/유지 대상과 10분 유효 변경안 생성 |
| `POST /v1/security-review/apply` | 정책을 다시 읽고 같은 변경안만 적용 |
| `POST /v1/security-review/verify` | 설정·앱 상태를 다시 확인 |

POST body는 `{ "revision": 2 }`, apply에는 `planId`를 추가한다. 대상 역할·계정·버킷·정책은 클라이언트가 지정할 수 없다. 기존 세션/CSRF 검사와 설정 파일의 사용자 allowlist를 사용한다. 권한 없는 사용자에게 404, 설정 없는 기능에는 503을 반환한다. 다른 기존 API는 그대로 동작한다.

상태는 `idle → gap → plan → applied → verified`로 진행한다. 오류/미확인/앱 준비 실패를 통과로 바꾸지 않는다. 적용 성공과 재확인 성공을 분리한다. plan은 적용 직전에 원본·대상 정책의 fingerprint를 비교하고, GCS 쓰기에는 etag를 보존한다. 다른 사용자의 변경이나 오래된 revision은 거부한다. 다른 계정의 바인딩과 앱 버킷은 변경하지 않는다.

DynamoDB CAS로 중복 요청을 막고, 최대 50개 이벤트에 서버 시각과 인증된 사용자 login을 남긴다. 브라우저 새로고침은 기록을 지우지 않는다. HTTP 연결이 끊겨도 실행 중 task를 유지한다. 프로세스 종료를 넘는 durable job은 아니며 3분이 지난 미완료 작업은 재조사가 필요하다.

`uiCanContinue`는 재확인 후 10분 동안만 유효하다. **기존 workflow/engine/approval/gate/cutover에는 연결하지 않는다.** 프런트 `/security`의 계속 버튼만 제어한다. 이 상태를 실제 트래픽 전환 승인으로 사용하면 안 된다.

## 조회 전용 연결

`readOnly`는 기본값이 `true`다. 조회와 재검사는 가능하고, 변경안 생성과 적용은 서버에서 403으로 거부한다. CloudProvider도 별도로 쓰기를 거부한다. 응답의 `readOnly`를 받아 프런트는 조회 전용 표시를 하고 변경안·적용·진행 버튼을 숨긴다. 기존 workflow와 마이그레이션 승인은 수정하지 않는다.

실제 연결에는 별도 GCP 조회 계정을 사용한다. 앱 서비스 계정의 권한을 변경하지 않고, 지정한 두 버킷에만 조회 계정의 읽기 권한을 추가한다. AWS Task Role에는 지정한 앱 역할의 `iam:GetRolePolicy`만 추가한다. 조회 계정에는 `storage.buckets.setIamPolicy`와 객체 읽기·쓰기·삭제 권한을 부여하지 않는다.

쓰기 시연을 다시 사용할 때만 별도 설정으로 `readOnly: false`와 적용 권한을 준비한다. 로컬 fixture 예시는 이 모드를 명시한다.

## ECS 연결

현재 런타임 조사(2026-11-08): `foxtrot-platform`의 control-plane·console·adapter는 ECS다. seed의 `chiikawa` 앱은 AWS EC2 `chiikawa-aws-3e3d`와 GCP Compute Engine `chiikawa-gcp-1975`를 가리킨다. 별도의 앱 ECS task는 없었다.

1. 새 control-plane 이미지를 빌드한다. 기존 seed/워크플로 구성은 바꾸지 않는다.
2. `config/security-review.example.json`을 실제 대상으로 채운다. 확인한 chiikawa 리소스는 `config/security-review.chiikawa.example.json`에 준비되어 있다. 이 예시의 허용 사용자는 SuperSon7 한 명이다. `application`은 seed와 같아야 한다. 운영 사용자 ID는 `/v1/me`의 `user.id`(`github:<id>`)로 제한한다.
3. ECS task environment에 `CONTROL_PLANE_SECURITY_REVIEW_CONFIG_JSON`을 설정한다. JSON 문자열을 직접 받으므로 별도 볼륨은 필요 없다. 파일을 쓰면 `CONTROL_PLANE_SECURITY_REVIEW_CONFIG`로 경로를 지정한다. 둘 다 있으면 JSON이 우선이다. 변경 후 task 재시작이 필요하다.
4. task role에 **해당 AWS 앱 역할 ARN의 `iam:GetRolePolicy`만** 추가한다. 현재 `foxtrot-control-plane-task`에는 이 권한이 없다. 추가할 정책 예시는 `config/security-review.ecs-task-policy.example.json`이다.
5. GCP 인증을 연결한다. ADC 또는 `gcpCredentialsRef`를 지원한다. `env:SECURITY_GCP_CREDENTIALS`는 ECS secrets로 주입한 JSON, `aws:<secret-id>`는 서버에서 Secrets Manager 조회다. 후자를 선택하면 task role에 그 secret의 `secretsmanager:GetSecretValue`와 필요한 경우 지정 KMS key의 `kms:Decrypt`가 필요하다. 개인 키는 일반 task environment/config/frontend 번들에 넣지 않는다.
6. 런타임 GCP 계정은 앱 버킷에 `storage.buckets.getIamPolicy`, 데모 버킷에 `storage.buckets.getIamPolicy`와 `storage.buckets.setIamPolicy`가 필요하다. 버킷 단위 custom role binding으로 제한할 수 있다. 준비용 계정의 project-wide 권한을 런타임에 그대로 요구하지 않는다.
7. ECS에서 IAM/Google API 및 seed의 GCP 앱 `/readyz`에 접근할 수 있어야 한다. GCP 앱 준비 확인은 5초 timeout이다.

이 PR은 ECS task role·task definition·서비스 배포를 자동 변경하지 않는다. 프런트와 백엔드 기능을 함께 배포해야 한다. 기존 콘솔 auth, CSRF, 프록시 설정을 그대로 사용한다. 기능이 미설정이면 화면에서 연결 필요를 안내한다.

## 실제 데모 준비와 재설정

사용자 앱의 실제 버킷에 더 넓은 권한을 부여하지 않는다. `extraBucket`은 `foxtrot-security-demo-`로 시작하는 별도 빈 버킷이어야 한다.

```sh
AWS_PROFILE=foxtrot uv run python -m control_plane.security_review.prepare \
  --config /path/to/security-review.json --project foxtrot-513413
```

준비 CLI는 전용 label, public access prevention, 빈 버킷을 확인하고 앱 계정의 `roles/storage.objectUser`만 추가한다. 버킷이 없으면 UBLA/PAP를 켜서 만든다. 기존 데이터(이전 버전 포함)가 있거나 전용 label이 없으면 중단한다. 준비 계정에는 생성/메타데이터 조회/객체 목록/IAM 조회·변경 권한이 별도로 필요하다. 재시연 시 같은 CLI로 추가 권한을 복원한다. 실제 앱 버킷, 네트워크, 기존 앱 인증은 변경하지 않는다.

2026-11-08 실제 연동 검증 대상:

| 항목 | 리소스 |
| --- | --- |
| AWS 역할 / 정책 | `chiikawa-aws-3e3d-app` / `workload-bucket` |
| AWS 앱 버킷 | `foxtrot-chiikawa-aws-3e3d` |
| GCP 앱 버킷 | `foxtrot-chiikawa-gcp-1975` |
| GCP 앱 계정 | `chiikawa-gcp-1975-app@foxtrot-513413.iam.gserviceaccount.com` |
| 별도 데모 버킷 | `foxtrot-security-demo-513413` |

로컬 프로세스에서 실제 CloudProvider/ReviewService로 조사→변경안→적용→재검사를 실행했다. 앱 준비 응답은 기존 gateway의 SSM 읽기 전용 명령으로 GCP 내부 `/readyz`에 요청해 전후 HTTP 200을 확인했다. AWS 선택 정책과 GCP 앱 버킷 IAM은 전후 동일했다. 검증 후 재시연을 위해 빈 데모 버킷의 추가 권한을 복원했다. ECS에 새 기능을 배포한 검증은 아직 아니다. 기존 provisioner 자격 증명은 이 로컬 검증에만 사용했고 출력·기록에 포함하지 않았다.

## 클라우드 없이 화면 통합 시연

```sh
CONTROL_PLANE_ENVIRONMENT=local \
CONTROL_PLANE_HOST=127.0.0.1 \
CONTROL_PLANE_AUTH_DISABLED=true \
CONTROL_PLANE_DYNAMODB_ENDPOINT=memory \
CONTROL_PLANE_SEED_PATH=config/security-review.seed.yaml \
CONTROL_PLANE_SECURITY_REVIEW_CONFIG=config/security-review.fixture.json \
TRAFFIC_SERVICE_TOKEN=local WORKLOAD_TOKEN=local \
uv run uvicorn control_plane.security_review.demo:create_demo --factory --host 127.0.0.1 --port 8112
```

프런트는 `VITE_API_MODE=real CONTROL_PLANE_PROXY_TARGET=http://127.0.0.1:8112 vp dev --host 127.0.0.1 --port 4212`로 실행하고 `/security`에 접속한다. 이 fixture는 실제 API/판정/저장 경로를 사용하지만 IAM/GCS/앱 응답은 명시적 예시다. 화면에 ‘예시 데이터’를 유지한다. 운영 환경에서는 fixture를 거부한다. 예시 서버는 migration worker를 실행하지 않고 외부 adapter에 연결하지 않는다. 브라우저 새로고침은 결과를 유지하며, 메모리 서버를 재시작하면 초기화된다.

## 이후 확장

현재는 seed의 앱 한 개를 서버에서 고정한다. 앱 선택, 전체 effective IAM 평가, 객체 허용/거부 테스트, durable jobs, 장기 감사 저장소는 별도 구현이다. AI는 비밀을 제거한 검사 근거의 설명/변경안 초안에만 붙일 수 있다. AI가 판정·권한 적용·전환 승인을 수행하지 않는다.
