# AWS 플랫폼에서 집 PC로 앱 배포

기준: 최신 `release/2`의 고정 앱 배포 정책. 온프렘 API는 `deployment/onprem_api.py`에
추가하고 기존 클라우드 배포 API와 시작 코드는 유지한다. 배포 실행 코드는 이 저장소의
`src/control_plane/deployment`와 `infrastructure`에 있다. 별도 IaC 서버를 추가로
실행하지 않는다. 이 문서의 명령은 운영자가 나중에 실행하는 절차이며, 구현 검증에서는
AWS apply, 원격 SSH, 컨테이너 기동을 실행하지 않았다.

기존 AWS ECS task definition·Secrets Manager 주입·IAM·공용 Tailscale ACL·보안 그룹·DNS는
이 변경에서 수정하지 않는다. 온프렘 등록은 기존 workRoot의 별도 파일로 전달한다.
등록 오류는 온프렘 기능만 비활성화하며 AWS/GCP의 설정과 기동 경로는 유지한다.

## 실행 구조와 지원 범위

- 플랫폼: AWS의 기존 control-plane, DB/Redis 프록시, 마이그레이션 어댑터를 사용한다.
- PC: Ansible이 PostgreSQL 17·Valkey·SeaweedFS를 준비하고, 별도 Compose 프로젝트가
  동일한 앱 이미지로 API와 worker를 실행한다. DB의 앱 테이블은 앱이 만든다.
- 두 Compose 프로젝트는 준비 환경의 Docker network로 연결된다. 앱 삭제는 데이터
  서비스와 볼륨을 삭제하지 않는다. 현재 등록 노드 하나에 활성 앱 배포 하나를 허용한다.
- 현재 호스트 검증은 Linux x86_64, 최소 2 vCPU·4 GiB RAM·20 GiB 여유 공간이다.
  Windows/macOS에서는 Linux x86_64 VM 안에 SSH·Tailscale·Docker를 함께 설치한다.
  ARM 또는 Docker Desktop 네이티브 경로는 이 구현의 검증 대상이 아니다.
- 앱 실행 계약은 기존 클라우드와 동일한 Python/FastAPI 템플릿:
  `app.main:app`, `python -m app.worker`, `/readyz`, `X-Served-By`다.
  임의 컨테이너 이미지를 자동 판별하는 기능은 없다.

집 공인 IP, 아파트 공인 IP, 공유기 포트 포워딩은 입력받지 않는다.
플랫폼은 등록된 PC의 Tailscale IP로 SSH와 앱 검사를 수행한다.

## 1. PC와 네트워크 준비

PC에 Python 3, OpenSSH 서버, Docker Engine 및 Compose v2가 있어야 한다.
전용 SSH 사용자가 sudo 없이 Docker를 실행할 수 있어야 한다. 이 권한은 호스트에서
강한 권한이므로 전용 계정을 사용한다. Ansible은 OS 패키지나 Tailscale을 설치하지 않는다.

PC를 플랫폼과 같은 Tailnet에 추가하고 AWS 관리 VPC의 subnet route를 수락한다.
Linux에서는 기존 로그인 상태에서 `sudo tailscale set --accept-routes=true`를 사용할 수 있다.
노트북 절전과 VPN 종료는 배포를 끊으므로 데모 중에는 비활성화한다.

| 경로 | 용도 |
| --- | --- |
| 플랫폼 → PC Tailnet IP:22 | SSH/Ansible/Compose |
| 플랫폼 → PC 앱 포트 | 실제 HTTP readiness 및 앱 식별 |
| 플랫폼 어댑터 → PC:5432,6379,8333,18888 | DB, Valkey, SeaweedFS S3 및 filer gRPC |
| PC/앱 컨테이너 → 관리 VPC:8000,6432,6379,8080 | heartbeat, DB/Redis 프록시, object route |
| PC/앱 컨테이너 → DNS 서버:53 TCP/UDP | 내부 서비스 이름 조회 |
| PC → 이미지 레지스트리:443 | 앱과 데이터 서비스 이미지 다운로드 |

현재 Tailnet ACL이 위 경로를 허용하는지 먼저 확인한다. 같은 Tailnet에 가입했다는
이유만으로 접근이 허용됐다고 가정하지 않는다. 코드와 설정 생성기는 ACL을 수정하지 않는다.
차단된 경로가 있으면 출발지·목적지·포트를 보고하고 기존 정책 관리자에게 필요한 규칙만
요청한다. 공용 ACL을 교체하거나 권한을 넓힌 장치 태그로 우회하지 않는다.
관리 게이트웨이의 기존 SNAT 경로와 VPC 허용 보안 그룹을 사용하며 PC별 SG 규칙을 추가하지 않는다.
플랫폼 private route table의 `100.64.0.0/10 → gateway` 경로는 기존 코드를 사용한다.
PC의 로컬 방화벽에서도 VPN을 통한 해당 포트 접근을 허용해야 한다.
AWS 앱 데이터로 직접 접속하는 경로가 별도로 필요한 프로파일은 AWS 앱 subnet route,
반환 경로와 그 데이터 서비스 보안 그룹까지 별도로 확인한다.

기존 `internal` split DNS와 관리 VPC의 기본 resolver를 그대로 사용한다. 기본 VPC라면
`10.30.0.2`이며, VPC CIDR을 바꿨다면 해당 VPC의 +2 주소를 사용한다. 관리 게이트웨이의
기본 SNAT 경로에서 이 resolver에 접근한다. 별도 DNS 서버를 만들 필요는 없다.
앱 설정의 `dnsServers`에도 이 주소를 넣어 컨테이너에서 내부 이름을 확인한다.
PC의 DNS 53 접근과 AWS 원본 DB의 5432 접근은 기존 ACL·보안 그룹·라우팅에서
별도로 확인해야 한다. 연결 검사가 실패해도 생성기가 플랫폼 네트워크를 변경하지 않는다.
[Tailscale AWS DNS 안내](https://tailscale.com/docs/reference/reference-architectures/aws)를 참고한다.

원본 PC의 host key는 PC에서 `/etc/ssh/ssh_host_ed25519_key.pub`를 직접 확인한다.
등록에는 `ssh-ed25519 AAAA...`의 두 필드만 넣는다. 사용자 키의 공개키와 혼동하지 않는다.
`id -u deploy`, `id -g deploy`로 SSH 사용자의 UID/GID도 확인한다.

## 2. 앱 소유권 확인과 설정 생성

플랫폼 seed에 설정된 고정 앱을 사용한다. `POST /v1/applications` 등록 단계는 없다.
모든 변경 API는 세션 쿠키, `Origin`, `X-CSRF-Token`, `Idempotency-Key`를 사용한다.
먼저 `GET /v1/applications`에서 앱 ID를 확인하고 아래처럼 소유권을 조회한다.

```http
GET /v1/applications/goods/deployment-targets
```

응답의 `orgId`를 설정 생성기의 `--org`에 전달한다.
PC가 아직 등록되지 않았다면 `onprem` 목록이 빈 것은 정상이다.
다른 사용자의 앱에는 이 노드를 노출하지 않는다.

`infrastructure/provisioner/onprem-app.example.json`을 비공개 작업 경로로 복사하고 수정한다.

| 설정 | 넣을 값 |
| --- | --- |
| `imagePrefixes` | 실제 앱 레지스트리 경로. 끝은 `/`, `:`, `@` 중 하나 |
| `dnsServers` | 기존 관리 VPC resolver IP. 기본 예: `10.30.0.2` |
| `CONTROL_PLANE_URL` / `WORKLOAD_SCOPE` | 플랫폼 heartbeat 주소와 해당 앱 scope |
| `DATABASE_URL` | 현재 앱의 고정 DB 프록시와 writer 사용자·DB 이름 |
| `REDIS_URL` | 현재 앱의 Redis 프록시. 인증이 필요하면 비밀 파일에서 전달 |
| `OBJECT_MIGRATION_SERVER` | 현재 object 어댑터 주소 |
| `S3_BUCKET` / `S3_FALLBACK_BUCKET` | 전환 전 실제 원본 버킷 이름 |
| `apiEnvironment.WORKLOAD_ACTOR` / `workerEnvironment.WORKLOAD_ACTOR` | 플랫폼 seed에 등록한 온프렘 actor 이름 |

PC는 ECS task role을 상속하지 않는다. 원본 S3에 앱이 접근한다면 그 앱의 제한된
S3 접근 키도 필요하다. private ECR/GHCR 등의 앱 이미지는 **SSH 사용자 계정**에
유효한 Docker registry 인증이 있어야 한다. ECR 로그인 토큰은 만료되므로 credential helper
등 갱신 방법을 준비하거나 데모에 접근 가능한 동일 이미지를 사용한다.

`app-secrets.json`은 `chmod 600`으로 보호하고 다음 값을 실제 환경에 맞게 채운다.
이 파일이나 생성 bundle을 저장소에 커밋하지 않는다.

```json
{
  "WORKLOAD_TOKEN": "REPLACE_WITH_PLATFORM_WORKLOAD_TOKEN",
  "APP_DB_PASSWORD": "REPLACE_WITH_EXISTING_PROXY_WRITER_PASSWORD",
  "AWS_ACCESS_KEY_ID": "REPLACE_IF_SOURCE_S3_REQUIRES_IT",
  "AWS_SECRET_ACCESS_KEY": "REPLACE_IF_SOURCE_S3_REQUIRES_IT"
}
```

사용하지 않는 항목은 지운다. SeaweedFS 앱용 키와 endpoint는 준비 결과에서
`STORAGE_SEAWEEDFS_ACCESS_KEY_ID`, `STORAGE_SEAWEEDFS_SECRET_ACCESS_KEY`,
`STORAGE_SEAWEEDFS_ENDPOINT`로 자동 전달하므로 별도로 복사하지 않는다.
DB 관리자 계정은 앱에 전달하지 않으며 `DB_HOST`도 설정하지 않는다.

`images.json`에는 실제로 사용할 세 이미지의 digest를 넣는다. 태그나 가짜 digest는
실행용 설정으로 사용할 수 없다. `--images-file`을 사용하면 생성기에 Docker가 필요 없다.

```json
{
  "postgres": "postgres@sha256:REAL_DIGEST",
  "valkey": "valkey/valkey@sha256:REAL_DIGEST",
  "seaweedfs": "chrislusf/seaweedfs@sha256:REAL_DIGEST"
}
```

```sh
python -m control_plane.deployment.tools.setup_onprem \
  --address 100.64.1.2 --ssh-user deploy --uid 1000 --gid 1000 \
  --host-key 'ssh-ed25519 REPLACE_WITH_PC_HOST_KEY' \
  --org app-REPLACE_FROM_API --node-id home-pc \
  --app-config /private/onprem-app.json --app-secrets /private/app-secrets.json \
  --app-image REGISTRY/APP@sha256:REAL_DIGEST \
  --images-file /private/images.json --output /private/foxtrot-pc
```

생성기는 SSH 키 쌍, DB/Valkey/SeaweedFS 계정, PostgreSQL TLS 인증서, inventory,
profile, `config.json`, `platform-bundle.json`을 생성한다. 기존 경로는 덮어쓰지 않는다.
`--app-image`는 서버의 `clouds.onprem.images.chiikawa-groupbuy`에 저장한다. 이미 이 값이
있는 `--platform-config`를 전달했다면 이미지 옵션은 생략할 수 있다. 생성 bundle에도
이미지를 포함하므로 플랫폼에서 별도로 수동 편집할 필요가 없다. 이미지가 없거나
허용된 레지스트리 밖이면 키·파일을 생성하기 전에 거절한다.
앱 포트는 서버 catalog의 기본값 80이다. 이미지·포트·템플릿·healthPath를 배포 요청에서
변경할 수 없다. 운영자가 catalog를 바꾸면 VM 포트 사용 여부와 기존 접근 정책도 확인한다.
생성한 `secrets/ssh_key.pub`만 PC의 해당 SSH 사용자 `authorized_keys`에 추가한다.
기존 키는 유지하고 권한은 `.ssh=700`, `authorized_keys=600`으로 설정한다.

## 3. 플랫폼에 등록

플랫폼의 **기존 workRoot** 아래 `onprem-registration/bundle.json`으로 생성한
`platform-bundle.json`을 전달한다. 현재 AWS 구성은 이미 EFS를
`/var/lib/foxtrot-provisioner`에 마운트하므로 최종 경로는 다음과 같다.

```text
/var/lib/foxtrot-provisioner/onprem-registration/bundle.json
```

운영자가 이미 사용할 수 있는 비공개 파일 전달 경로를 사용한다. 디렉터리는 0700,
파일은 0600, 소유자는 플랫폼 프로세스 사용자여야 한다. bundle은 64 KiB 이하이며
symlink는 허용하지 않는다. 임시 파일로 완전히 전달한 뒤 이름을 바꿔 교체한다.
기존 전달 경로가 없다면 등록 전달이 미완료임을 보고한다. 이를 위해 ECS Exec·IAM·
task definition·Secrets Manager 설정을 자동으로 추가하지 않는다.

플랫폼은 시작할 때 이 선택 파일을 읽고 온프렘 전용 디렉터리에 자격 증명을 보관한다.
기존 AWS/GCP 설정을 보존하면서 온프렘 노드·서버 이미지·inventory를 추가한다.
파일이 없으면 기존 설정으로 시작한다. 파일 권한·JSON·catalog·드라이버 초기화에
문제가 있으면 온프렘만 비활성화하고 비밀값 없는 경고를 남긴다. 온프렘 전용 API는
`503 ONPREM_CONFIGURATION_UNAVAILABLE`를 반환하며 기존 클라우드 API는 계속 제공한다.
파일 수정 후 반영에는 플랫폼 재시작이 필요하다. 자동 재시작이나 운영 환경 변경은 하지 않는다.
현재 bundle 하나는 PC 한 대 등록용이며 원문이나 개인키를 Git·tfvars에 넣지 않는다.

코드 push만으로 실행 중인 플랫폼에 API가 추가되지는 않는다. 이 브랜치의 플랫폼 이미지는
기존 릴리스 절차에서 배포하고 등록 파일도 그 절차에 맞춰 반영한다.

별도 로컬 controller를 쓰는 경우에는 생성 시 `--platform-config`로 기존 설정을
합치고 생성된 `config.json`을 `PROVISIONER_CONFIG`로 지정한다.
실제 플랫폼 배포에는 `--local`을 쓰지 않는다. 그 옵션은 loopback 인프라 검증 전용이다.

## 4. 준비 후 앱 배포

데모에서는 아래 준비 요청을 미리 실행해 `INFRA_READY`의 환경 ID를 보관한다.
시연 시 로그인한 사용자가 `POST /v1/workflows`로 등록된 AWS 원본과 온프렘 대상을
선택하면 마이그레이션이 `PLANNED`로 생성된다. 앱을 띄울 때 아래 배포 요청에
응답의 `workflow.id`를 `workflowId`로, 보관한 환경 ID를 `target.preparationId`로 넣는다.
같은 요청을 재시도할 때는 같은 `Idempotency-Key`를 쓴다.
앱 배포 결과는 `READY`로 확인하고, 데이터 이전 진행은 워크플로의 별도 승인 단계에서 제어한다.

```http
POST /v1/applications/goods/onprem-preparations
Idempotency-Key: goods-home-preparation-1
{"nodeId":"home-pc","mode":"apply"}
```

202 응답의 `Location`으로 job을 조회한다. 준비 환경은
`GET /v1/provisioning/onprem/environments/{environmentId}`로 조회한다.
`INFRA_READY`가 되면 응답의 ID를 앱 배포의 `preparationId`로 사용한다.
준비 전이거나 다른 앱의 준비 결과라면 배포를 거절한다.

```http
POST /v1/applications/goods/deployments
Idempotency-Key: goods-home-app-1
{
  "cloud":"onprem",
  "workflowId":"wf-REPLACE_FROM_MIGRATION_REQUEST",
  "target":{"nodeId":"home-pc","preparationId":"env-REPLACE_FROM_PREPARATION"},
  "app":{"apiWorkers":4,"workerCount":2},
  "mode":"apply"
}
```

앱 배포 상태는 `GET /v1/provisioning/environments/{environmentId}`에서 조회한다.
이 앱 배포 ID와 데이터 서비스 준비 ID는 서로 다르다.
독립적으로 앱만 배포할 때는 `workflowId`를 생략한다. 기존 워크플로에 로그인 이름만
저장돼 있고 생성자 계정 ID를 확인할 수 없다면 그 요청을 앱 배포에 연결할 수 없다.
온프렘 배포는 `targetEnv: "onprem"`인 요청에 연결한다. 이 요청의 내부 저장 기록에만
생성자의 계정 ID를 보관한다. 콘솔에는 기존과 같이 `createdBy`로 로그인 이름을 표시한다.
`mode:"plan"`을 사용하면 계획 뒤 `POST .../apply`로 실행을 분리할 수 있다.
데이터 준비의 apply는 `expectedRevision`과 응답의 `plan.planId`도 요구한다.

`READY`는 준비된 인프라, API/worker 컨테이너 실행, Docker healthcheck,
컨테이너의 등록 endpoint 접속, **플랫폼에서 직접 보낸 HTTP 200과 X-Served-By 일치**를
확인한 상태다. 이 검사는 전체 actor의 HOLD 확인을 대신하지 않는다.
현재 API 4개/worker 2개이면 seed의 온프렘 actors도 각각 4/2로 등록해야
마이그레이션 시 프로세스 정지 확인이 맞게 동작한다.
온프렘 준비·배포 API가 migration seed나 adapter resource를 자동 생성하지는 않는다.

## 장애 복구와 삭제

- apply/delete 도중 SSH가 끊기면 `NEEDS_RECONCILIATION`으로 두고 중복 배포를 막는다.
  연결 복구 뒤 `POST /v1/provisioning/environments/{appId}/reconcile`로 읽기 전용
  상태 확인을 수행한다. 이미 삭제가 끝났다면 `DESTROYED`로 복구한다.
- 앱 검사만 실패하면 인프라 준비 증거를 유지한다. `check-app`으로 다시 검사한다.
- `POST /v1/provisioning/environments/{appId}/destroy`는 해당 앱 컨테이너만 제거한다.
  진행 중 workflow가 연결된 앱은 제거하지 않는다. 데이터 서비스는 남긴다.
- 데이터 서비스 정리는 앱 삭제 후 준비 환경 `.../plan`에
  `{"expectedRevision":현재번호,"intent":"cleanup"}`을 보내고, 받은 planId로 apply한다.
  데이터 볼륨은 보존된다. 앱이 연결된 준비 환경의 재설정/정리는 거절한다.
- 등록 주소·SSH 키·profile을 실행 중 임의 교체하지 않는다. 계획 이후 변경은 거절한다.
  원격 서버가 꺼진 경우 상태 확인 없이 작업 성공이나 삭제 완료로 처리하지 않는다.

## 배포 준비와 실제 마이그레이션의 경계

앱 handoff의 `resources`는 DB/Valkey/SeaweedFS endpoint와 **비밀 참조**를 제공한다.
`migrationReadiness.status=NOT_EVALUATED`는 별도로 유지한다.
`READY`만 보고 데이터 전환이나 트래픽 전환을 승인하면 안 된다.

| 남은 연결 | 위치와 필요한 작업 |
| --- | --- |
| DB RDS → self-managed PostgreSQL | 선택할 DB 어댑터 profile의 RDS → SELF_MANAGED 지원·TLS·writer fence 검증 |
| 앱 테이블 준비 | 사용자 앱의 schema bootstrap. Ansible이나 이 executor가 앱 테이블을 만들지 않음 |
| Valkey / SeaweedFS 대상 등록 | 각 어댑터에 handoff endpoint와 credential 참조 등록. 어댑터의 secret store에서 해석 가능해야 함 |
| HOLD 및 프로세스 수 | control-plane seed에 온프렘 actors/target resource를 등록하고 실제 heartbeat·writer fence 확인 |
| 사용자 웹 트래픽 | 온프렘 origin용 Cloudflare Tunnel과 traffic pool 연결·검증. 사설 Tailscale IP를 공용 origin으로 직접 등록하지 않음 |

HOLD는 실제 플랫폼 지시와 앱 보고로 확인해야 한다. 준비 handoff에 있는 초기 모드 값만으로
확인했다고 간주하지 않는다. 이 변경은 DB 어댑터 profile 확장, Cloudflare origin 생성,
실제 데이터 복제·전환까지 완료했다고 표시하지 않는다.
