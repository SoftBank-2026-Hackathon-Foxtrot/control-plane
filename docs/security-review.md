# 버킷 권한 비교와 데모 권한 수정

AWS의 지정 인라인 정책, GCP 앱 버킷 접근, 별도 데모 버킷의 추가 접근을 비교한다. 전체 IAM 동등성이나 보안 인증을 판정하지 않는다. AWS의 다른 정책, GCP 상속 권한·그룹·객체 ACL, DB·Redis·서버 보안은 검사하지 않는다. 공개 주체(`allUsers`, `allAuthenticatedUsers`)가 있으면 미확인으로 남기고 자동 변경안을 만들지 않는다.

## 화면과 API

1. `POST /v1/security-review/scan`: 실제 AWS/GCP 정책과 GCP 앱 `/readyz`를 조회한다.
2. `POST /v1/security-review/plan`: 추가 접근만 제거하는 변경안을 만든다. 계정, 제거할 데모 버킷·역할, 유지할 앱 버킷과 10분 유효 시각을 표시한다. 조회 전용에서도 변경안은 확인할 수 있다.
3. `POST /v1/security-review/apply`: 사용자가 변경안을 확인하고 적용한다. 서버는 정책을 다시 읽어 fingerprint를 비교하고 앱 응답을 재확인한다. 지정된 빈 데모 버킷의 label·공개 접근 방지·비어 있음을 확인한 뒤, etag를 포함해 해당 앱 계정의 지원되는 객체 역할만 제거한다. 다른 계정의 권한과 앱 버킷은 유지한다.
4. `POST /v1/security-review/verify`: 정책과 앱 준비 응답을 다시 조회한다. 적용 완료만으로 재검사 성공을 표시하지 않는다.

GET `/v1/security-review`와 화면 자동 갱신은 저장된 결과만 읽는다. POST는 현재 `revision`, 적용에는 `planId`도 필요하다. 기존 로그인·Origin·CSRF와 보안 설정의 사용자 allowlist를 사용한다. 대상은 서버 설정에서만 지정한다.

조회 실패·작업 중·적용 직후에는 과거 검사 항목을 미확인으로 표시한다. 마지막 수집 시각은 남는다. DynamoDB의 별도 `SECURITY_REVIEW#…` 항목에 결과와 최대 50개 사용자·시각 기록을 저장하며 기존 앱 DB 스키마는 바꾸지 않는다. CAS가 중복 작업을 막는다. 프로세스 종료를 넘는 작업 큐는 아니며 3분이 지난 미완료 작업은 재조회한다.

**기존 배포, workflow, 마이그레이션 방향, 승인, 트래픽 전환은 변경하지 않는다.** 화면의 마이그레이션 목록 링크는 실제 전환 승인과 별개다. `/readyz`는 객체 CRUD 테스트가 아니다.

## 조회 계정과 적용 계정

기본 `readOnly: true`는 조회·변경안 미리보기만 허용한다. 적용은 서버와 CloudProvider에서 거부한다. 실제 적용은 `readOnly: false`, 별도 `gcpApplyCredentialsRef`, `foxtrot-security-demo-` 접두사의 데모 버킷을 명시해야 한다.

- 조회 인증: `gcpCredentialsRef: env:SECURITY_GCP_CREDENTIALS`.
- 적용 인증: `gcpApplyCredentialsRef: env:SECURITY_GCP_APPLY_CREDENTIALS`.
- AWS Task Role: 지정 앱 역할의 `iam:GetRolePolicy`.
- 조회 계정: 두 버킷의 `roles/iam.securityReviewer`, 데모 버킷에만 안전 조건 조회용 `roles/storage.legacyBucketReader`.
- 적용 계정: 데모 버킷에만 `roles/iam.securityAdmin`. 이 버킷의 IAM/ACL 관리 권한이며 객체 내용 읽기·생성·삭제 권한이나 앱 버킷 접근은 부여하지 않는다. 프로젝트 전체 역할은 추가하지 않는다. 코드가 허용하는 변경은 앱 계정의 추가 직접 권한 제거뿐이다.
- ECS execution role: 두 credential secret의 `GetSecretValue`. 키 값은 ECS secrets로 주입하며 설정 JSON·프런트·로그에 넣지 않는다.

현재 연결 대상은 `config/security-review.chiikawa.example.json`에 있다. 허용 사용자는 SuperSon7 한 명이다. AWS 앱 역할은 `chiikawa-aws-3e3d-app`, AWS 앱 버킷은 `foxtrot-chiikawa-aws-3e3d`, GCP 앱 버킷은 `foxtrot-chiikawa-gcp-1975`, 수정 대상은 `foxtrot-security-demo-513413`이다. 앱 계정은 `chiikawa-gcp-1975-app@foxtrot-513413.iam.gserviceaccount.com`이다.

## 배포 연결 유지

ECS에는 `CONTROL_PLANE_SECURITY_REVIEW_CONFIG_JSON`과 위의 두 secret을 전달한다. 파일 설정은 `CONTROL_PLANE_SECURITY_REVIEW_CONFIG`를 사용할 수 있고, JSON이 우선한다. 앱 이름은 seed와 같아야 한다. 환경변수 변경 후에는 task 재시작이 필요하다.

Terraform의 `security_review` 입력에 설정 JSON·앱 역할 ARN·기존 secret ARN을 전달한다. `infrastructure/environments/platform/aws/security-review.tfvars.example.json`을 해당 환경의 실제 tfvars에 포함하거나 `-var-file`로 매번 지정한다. 기본값 null은 기능 미설정이므로 이미 연결한 환경에서 이 입력을 빼고 적용하면 연결을 잃는다. 이 입력은 GCP 계정/키를 생성하거나 마이그레이션 seed를 수정하지 않는다. 기존 수동 IAM policy를 Terraform으로 관리할 때는 state를 확인하고 필요하면 import한다. 전체 플랫폼 `apply` 전에 다른 변경이 섞이지 않았는지 plan을 확인한다.

운영 이미지를 갱신할 때는 현재 실행 중인 이미지 digest와 task definition을 기준으로 보안 코드만 추가한다. 다른 작업의 이미지나 seed를 덮어쓰지 않는다.

## 데모 재설정과 로컬 확인

데모 버킷을 다시 준비할 때는 별도의 준비 계정과 설정으로 다음을 실행한다. 준비 계정의 권한을 운영 적용 계정에 추가하지 않는다. `readOnly: true`는 인증·네트워크 요청 전에 거부된다.

```sh
uv run python -m control_plane.security_review.prepare --config /path/to/setup-config.json --project foxtrot-513413
```

준비 설정의 읽기·적용 참조에는 필요한 준비 계정을 연결한다. 전용 label·공개 접근 방지·빈 버킷(이전 버전 포함)을 확인하고 앱 계정의 objectUser 권한을 복원한다. 앱 버킷은 수정하지 않는다.

로컬 예시는 `config/security-review.fixture.json`, `config/security-review.seed.yaml`과 `control_plane.security_review.demo:create_demo`를 사용한다. GCP fixture는 로컬에서만 허용되며 화면에 예시 데이터로 표시한다. 실제 계정·비밀 없이 scan→plan→apply→verify를 검증할 수 있다.

## 객체 접근 검증과 운영 인계

/security에서 AWS → GCP와 온프렘 샌드박스를 선택한다. 두 경로 모두 주문 앱의 영수증 접근, 업무 외 버킷 접근, 복사 계정 영향을 살펴본다. 근거의 출처와 승인 효과는 다르다.

| 대상 | 조사와 검증 | 사람이 결정하는 내용 |
| --- | --- | --- |
| AWS → GCP | 지정 AWS 정책과 GCP 직접 IAM을 조회한다. 선택적으로 두 실제 계정의 객체 요청을 추가한다. 같은 앱의 활성 GCP workflow와 객체 migration ID를 조사 시점에 기록한다. | 변경안을 보고 기존 데모 버킷 IAM 변경을 승인한다. 적용 직전에 정책·준비 응답·설정된 실측·활성 workflow 식별자와 단계가 여전히 같은지 검사한다. 이후 verify에서 정책과 실측을 다시 확인한다. |
| 온프렘 | 배포 번들의 앱·복사 계정 권한과 handoff의 버킷 이름을 읽는다. 일회용 SeaweedFS 두 개에서 기존 설정과 변경 설정을 각각 기동하고 합성 객체만 사용한다. | 시험 성공 후 10분 안에 승인 또는 보류한다. 승인 시 앱 권한 변경 패치와 역방향 패치를 다운로드할 수 있다. 실제 호스트 반영은 운영자의 배포 절차에서 수행한다. |

### GCP 실측 연결

기존 리뷰 설정에 다음 선택 항목을 추가한다. `probes`를 생략한 기존 설정은 저장 키와 조회 동작을 유지한다. 이 항목을 추가하면 해당 설정의 별도 리뷰 이력을 시작한다.

```json
{
  "probes": {
    "appSecretRef": "env:SECURITY_GCP_APP_PROBE",
    "copySecretRef": "env:SECURITY_GCP_COPY_PROBE",
    "copyEmail": "copy@example-project.iam.gserviceaccount.com"
  }
}
```

앱 버킷에 비어 있지 않은 합성 객체 `receipts/security-review-canary.json`을 미리 준비한다. 두 secret은 지정된 앱·복사 서비스 계정의 자격 증명이다. 서버는 계정 이메일을 대조하고 읽기 전용 OAuth 범위로 시험한다. 객체 본문과 키는 결과에 보관하지 않는다. 서버는 빈 목록 응답도 허용으로 분류한다. 조회 오류, 객체 부재, 인증 오류, 원인이 불명확한 403은 미확인으로 남긴다. 지정 앱이 영수증을 읽지 못하거나, 복사 계정이 카나리를 읽지 못하거나, 추가 버킷 직접 권한과 실측이 일치하지 않으면 변경 적용 또는 재검사 완료로 진행하지 않는다. 카나리 읽기는 해당 계정의 읽기 능력만 확인한다. 실제 작업의 전체 진행률·쓰기 성공·상속 권한의 부여 경로까지 보증하지 않는다.

### 온프렘 시험 실행

1. 실행 호스트에 배포 도구와 같은 SeaweedFS 4.48 바이너리를 준비한다. `CONTROL_PLANE_SECURITY_WEED_BINARY`에 절대 경로를 설정한다. 샌드박스 프로세스의 임시 디렉터리, 루프백 포트와 실행 권한이 필요하다. 기본 이미지는 바이너리를 포함하지 않는다.
2. 기존 리뷰 사용자 allowlist를 재사용한다. GCP 없이 온프렘만 연결하려면 `CONTROL_PLANE_SECURITY_REHEARSAL_USERS`에 허용된 로그인 사용자 ID를 쉼표로 구분해 지정한다. 사용자 ID는 기존 세션의 `/v1/me`에서 확인한다.
3. `GET /v1/security-review/onprem/rehearsal`로 상태와 revision을 읽는다. 같은 경로의 `/run`, `/approve`, `/hold`에 현재 revision을 POST한다. 기존 세션·Origin·CSRF 및 저장소 CAS를 사용한다. 요청으로 호스트, 계정, 명령, 실행 파일 또는 버킷을 지정할 수 없다.
4. 설정마다 앱 계정으로 합성 영수증을 쓰고 읽어 내용을 대조한 뒤 별도 버킷 목록을 조회한다. 복사 계정으로 합성 객체를 쓰고 CopyObject를 실행한 뒤 읽은 내용을 대조한다. 기본 설정에서 세 요청이 허용되고, 변경 후 업무 외 버킷 조회만 거부되어야 승인할 수 있다.
5. 운영자는 승인된 RFC 6902 패치의 계정과 기존 권한 test 연산을 모두 확인한 뒤 배포 원본에 반영한다. 파일 전체를 덮어쓰지 않는다. 실제 호스트의 적용과 재시작, 운영 앱·복사 작업 재검사는 별도 단계다. 설정이 이미 달라졌다면 해당 패치를 적용할 수 없으므로 원본과 다시 비교한다. 복구 패치도 동일하게 현재 값부터 검사한다.

기본 후보는 규칙으로 생성한다. 선택적으로 `CONTROL_PLANE_SECURITY_BEDROCK_MODEL`과 해당 모델의 Bedrock 호출 권한 및 리전을 설정하면, 모델이 합성 시험 결과를 보고 앱의 허용 동작 후보를 고른다. 후보는 서버가 허용한 세 동작만 담을 수 있으며 항상 영수증 버킷으로 제한한다. 첫 후보가 실패하면 한 번 더 샌드박스 근거를 보내 재시험한다. 모델 호출 실패는 실패 상태로 기록한다. 승인과 보류 기록에는 검토한 결과·사용자·시각·revision이 포함된다. 최근 20개 기록을 보존한다. 완료된 시험을 재현하는 도구이며 실제 온프렘 호스트나 운영 중인 복사 작업의 실시간 검사와 구분한다.

### 확인 순서

로컬에서는 기존 seed, fixture와 메모리 저장소로 `control_plane.security_review.demo:create_demo`를 실행하고, 프론트의 `CONTROL_PLANE_PROXY_TARGET`을 그 서버로 연결한다. AWS → GCP 화면만 예시 데이터를 사용한다. 온프렘 샌드박스는 위 실행기가 있어야 실제 요청을 수행한다. 먼저 샌드박스에서 두 권한 설정의 응답과 패치를 검토한다. 실제 GCP 환경에서는 읽기 전용 scan과 계정·버킷·결과를 먼저 확인하고 기존 writer 연결에서만 apply를 사용할 수 있다.

컨트롤 플레인 저장소에서 PowerShell로 로컬 데모를 실행하는 예다. 실행기 경로는 설치 위치에 맞춘다. 이 메모리 데모의 기록은 서버 재시작 시 초기화된다.

```powershell
$env:CONTROL_PLANE_ENVIRONMENT = 'local'
$env:CONTROL_PLANE_HOST = '127.0.0.1'
$env:CONTROL_PLANE_AUTH_DISABLED = 'true'
$env:CONTROL_PLANE_DYNAMODB_ENDPOINT = 'memory'
$env:CONTROL_PLANE_SEED_PATH = 'config/security-review.seed.yaml'
$env:CONTROL_PLANE_SECURITY_REVIEW_CONFIG = 'config/security-review.fixture.json'
$env:CONTROL_PLANE_SECURITY_WEED_BINARY = 'C:\tools\seaweedfs-4.48\weed.exe'
$env:TRAFFIC_SERVICE_TOKEN = 'local'
$env:WORKLOAD_TOKEN = 'local'
uv run uvicorn control_plane.security_review.demo:create_demo --factory --host 127.0.0.1 --port 18112
```

프론트 저장소의 두 번째 터미널에서 `VITE_API_MODE=real`, `CONTROL_PLANE_PROXY_TARGET=http://127.0.0.1:18112`를 환경변수로 설정하고 `vp dev --host 127.0.0.1 --port 14212`를 실행한다. 브라우저에서 `/security#onprem`을 열어 시험 → 승인/보류 → 패치 저장을 확인한다.
