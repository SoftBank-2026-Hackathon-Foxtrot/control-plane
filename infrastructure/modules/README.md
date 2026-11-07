# 모듈 사용 안내

플랫폼은 `environments/aws` 또는 `environments/gcp` root를 실행합니다. 하위 모듈은 root의 검증된 입력을 받는 내부 구성 요소입니다. 각 모듈의 `variables.tf`는 입력/설명, `outputs.tf`는 출력, `main.tf`는 실제 자원을 정의합니다. Redis secret 변수는 민감값 validation과 함께 해당 모듈 `main.tf`에 있습니다.

| 모듈 | 주요 입력 | 주요 출력 |
|---|---|---|
| metadata | deployment_id, metadata | 공통 추적 필드 |
| aws/vpc | CIDR, AZ | VPC, public/private subnet IDs |
| aws/security_groups | VPC, 웹 포트/CIDR, 관리자 CIDR | app/db/cache SG IDs |
| aws/iam | 이름, 앱 bucket ARN | role ARN, instance profile |
| aws/ec2 | AMI, 사양, 디스크, subnet, SG, identity | instance ID, public/private IP |
| aws/rds | private subnets, SG, 사양, DB 이름 | DB endpoint, master secret ARN |
| aws/elasticache | private subnets, SG, 사양, AUTH token | endpoint, TLS port |
| aws/s3 | 고유 bucket 이름 | bucket 이름/ARN |
| gcp/network | subnet CIDR, PSA 범위, region | VPC/subnet ID, PSA 범위 이름 |
| gcp/firewall | VPC, 앱 SA, 웹/관리 CIDR, PSA CIDR | 웹 firewall 이름 |
| gcp/iam | project, 이름, 앱 bucket | 앱 SA email |
| gcp/compute | image, 사양, subnet, SA | VM ID, public/private IP |
| gcp/cloud_sql | VPC/PSA, 사양, DB 이름, 앱 SA | private IP, connection name, IAM DB username |
| gcp/memorystore | VPC/PSA, memory, HA | endpoint, port, CA certs |
| gcp/storage | 고유 bucket 이름, region | bucket 이름/URL |

## 확장 원칙

관리망 연결은 root의 `proxy_cidrs`와 AWS `migration_source_cidrs`로 지정합니다. `enable_cloudflare_tunnel`은 outbound 7844만 추가합니다. 상세 의미와 기존 라우팅 전제는 [demo 배포 연결](../docs/demo-deployment.md)을 따릅니다. Redis 모듈은 이전에 필요한 KEA 알림과 noeviction을 고정합니다.

1. 새로운 사양 후보: `catalog/sizing-policy.json`과 root의 sizing validation을 함께 수정합니다.
2. 새로운 네트워크 경로/권한: 사람의 구성 변경으로 검토합니다. AI JSON에 임의 HCL/IAM/user-data 필드를 추가하지 않습니다.
3. 다른 workload: 기존 모듈을 조합하는 별도 root를 만들고 별도 state를 사용합니다.
4. 하위 모듈 직접 사용: root에 있는 네트워크·메타데이터·사양 검증도 재사용해야 합니다. 하위 모듈 자체가 플랫폼 정책 전체를 보장하지 않습니다.
5. 모듈 변경 후 두 root의 fmt/validate/mock tests를 실행합니다. 실제 provider/API 특성은 실환경 plan과 승인된 검증으로 확인합니다.

서비스 모듈 안에서 provider credentials를 정의하거나 `local-exec`로 임의 클라우드 명령을 실행하지 않습니다. 앱 설치는 별도 배포 도구가 수행하도록 인계 정보를 제공합니다.
