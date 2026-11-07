# 앱 시스템 메트릭

CPU·메모리·디스크 I/O는 앱 컨테이너에서 수집합니다. 서버 전체 `system_*` 지표를 대신 조회하지 않습니다. VM·온프렘의 격리된 컨테이너 네트워크는 컨테이너별로, Fargate·Cloud Run의 공유 네트워크는 앱 인스턴스별로 표시합니다.

개인 배포는 로그인 사용자의 소유권을 배포 저장소에서 확인합니다. 별도 프로비저너가 관리하는 공용 앱은 control-plane 시드의 `systemDeployments`에 등록합니다.

```yaml
application: chiikawa
systemDeployments:
  - environmentId: env-784e02b9c9d5
    deploymentId: chiikawa-aws-3e3d
    ownerId: chiikawa
    environment: aws
```

`environmentId`, `deploymentId`, `ownerId`, `environment`는 실제 배포 저장소의 `id`, `deployment_id`, `org_id`, `cloud`와 일치해야 합니다. 환경은 시드의 `environments`에 있어야 합니다. 중복 등록과 빈 ID는 시작 시 거부합니다. 다른 앱을 추가하려면 해당 앱의 시드와 접근 제어부터 분리해야 합니다.

공용 등록은 **이 시드 앱을 보는 로그인 사용자 전체에게 해당 배포의 메트릭 조회만 허용**합니다. 개인 소유 배포를 공용 목록에 넣지 않습니다. 배포·삭제 권한과 저장소의 소유권은 바뀌지 않습니다. 등록을 제거하고 재시작하면 공용 조회 권한이 사라집니다. 프로비저너 SQLite를 control-plane에 공유할 필요가 없습니다.

`GET /v1/applications/{app}/system-metrics/deployments`는 최근 30초 이내 수집된 등록 대상만 반환합니다. 공용 대상은 `environmentId: shared:env-…`, `access: shared`, 개인 대상은 `access: personal`입니다. 선택한 값을 `/instances`와 시계열 API의 `deployment`에 그대로 전달합니다.

화면의 배포 선택은 `?include_inactive=true`를 사용합니다. 이 경우 수집 전·중단된 등록 대상도 반환하며 `collection`은 `REPORTING`, `NOT_REPORTING`, `UNKNOWN`입니다. 백엔드 오류나 미설정은 `UNKNOWN`이며 수집 정상으로 표시하지 않습니다. 개인 배포의 소유권 검사와 삭제 대상 제외, 공용 등록 범위는 동일합니다. 이 옵션은 인스턴스·시계열의 조회 범위를 넓히지 않습니다.

모든 조회는 `application.id`, `foxtrot.owner.id`, `deployment.id`, `deployment.environment.name`, `service.name=foxtrot-app-runtime`으로 한정합니다. 시계열은 `service.instance.id`까지 한정합니다. 같은 앱 이름이나 같은 소유자라는 이유로 다른 배포를 포함하지 않습니다.

## 운영 반영

1. `systemDeployments`를 지원하는 control-plane 이미지와 시드를 함께 배포합니다. 구버전은 새 필드를 거부합니다.
2. Collector/Prometheus에 owner·deployment·environment·instance·network scope 라벨을 보존하는 설정을 배포합니다.
3. 기존 앱에도 컨테이너 수집을 지원하는 demo 이미지와 정확한 OTEL ID를 적용합니다. 프로비저너 설정만 바꿔도 기존 앱이 갱신되지는 않습니다.
4. 인증된 `/deployments` 응답에서 기대한 배포만 보이는지 확인하고, 각 `/instances`에서 `stale: false`와 CPU·메모리·I/O·네트워크 값을 확인합니다. 수집이 없으면 0 대신 빈 결과로 남아야 합니다.

IaC의 `docs/shared-system-metrics.md`에 기존 앱 설정 생성과 검증 순서를 정리했습니다. 마이그레이션 상태·로그·트레이스는 이 시스템 메트릭 등록과 별도로 workflow 단위로 조회합니다.
