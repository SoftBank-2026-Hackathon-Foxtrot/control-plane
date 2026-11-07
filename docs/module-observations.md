# 모듈 관측값을 콘솔로 전달

`GET /v1/workflows/{id}`의 리소스 응답은 모듈 공통 API가 이미 보고한 데이터를 전달한다. 전환 판단과 명령 실행은 기존 엔진에 남아 있다. 이 변경은 신규 앱 등록·인프라 생성·배포·OTEL 수집을 구현하지 않는다.

## 전달하는 값

- `details.redis.proxy`: 쓰기 정지 여부, 처리 중 쓰기, 열린 트랜잭션, Pub/Sub 연결 수와 모듈 관측 시각.
- `details.redis.sync`: 모듈이 보고한 실행·정지, 복제 방향, 처리 건수, backlog, 지연, 오류와 관측 시각.
- `details.database`, `details.object`, `progress`, `replication`: 모듈이 제공한 관측값. 누락된 수치·전체 복사량을 0이나 추정값으로 채우지 않는다.
- `checks`, `readiness`, `rollback`, `businessWriteAccess`, 작업·전환 식별자: 기존 모듈의 보고. 검사 결과 원본은 유지하고, `currentResult`는 관측이 최신이고 `validUntil`이 남아 있을 때만 원본 판정을 사용한다.

모듈이 `streams`를 보고하면 해당 기록을 사용한다. Redis가 별도 streams 없이 `sync.running`, `sync.halted`, `sync.direction`을 보고하면 이를 일반적인 `REDIS_SYNC` 스트림으로 정규화한다. 초기 복사·키 알림 구독 같은 더 세부적인 작업의 시작/완료를 추측하지 않는다. 방향이 없으면 스트림 방향을 만들지 않는다.

## 조회와 신선도

미완료 작업의 상세 조회는 리소스별 상태 GET을 병렬로 수행한다. 모듈당 2초 안에 응답하지 않거나 조회에 실패해도 다른 모듈과 상세 화면 전체를 실패시키지 않는다. 저장된 마지막 관측값을 반환하면서 `observation.collectionError`와 `STALE` 또는 `UNAVAILABLE`을 표시한다. 연결 오류의 URL·인증 정보는 응답에 넣지 않는다.

`observedAt`은 모듈이 보고한 시각이다. `observation.collectedAt`은 CP가 성공적으로 수집한 시각이다. 둘을 혼용하지 않는다. 모듈 관측은 15초 이내일 때 최신으로 분류하며, 시각이 없거나 미래로 크게 어긋나면 `UNKNOWN`이다. Proxy·Worker는 각자의 관측 시각을 따로 평가하고 모듈이 명시한 UNKNOWN·STALE·UNAVAILABLE을 더 확실한 상태로 올리지 않는다.

조회는 엔진의 리소스 기록·워크플로 버전·명령을 변경하지 않는다. 완료·중단 완료된 작업은 모듈을 재조회하지 않고 기존에 엔진이 저장한 마지막 스냅샷을 반환한다. 이후 다른 이전에서 바뀐 모듈 상태가 완료 작업 화면에 섞이지 않는다.

## 검증

`tests/test_resource_observations.py`에서 같은 phase 안의 정지 여부·backlog 변경, 처리량의 원본 유지, 전체 수량 미보고, 오래되거나 불명인 관측, Worker 오류·정지, 조회 실패·시간 초과, 완료 이력과 API 조회의 엔진 상태 불변성을 검사한다. 이 검사는 모듈 응답과 API 계약에 대한 검증이며 실제 클라우드 전환 완료를 뜻하지 않는다.
