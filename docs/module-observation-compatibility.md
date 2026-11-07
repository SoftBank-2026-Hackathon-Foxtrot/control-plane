# 모듈 관측 API 호환성 확인 — 2026-11-08

팀원의 실제 작업 디렉터리는 변경하지 않고 원격 브랜치 사본을 별도 worktree에서 합쳐 확인했다. 다른 브랜치의 배포·승인·프로비저닝 기능을 이 PR로 가져오지는 않았다.

## 확인한 기준

| 저장소 / 원격 기준 | 커밋 | 결과 |
| --- | --- | --- |
| control-plane develop | e49bb20 | 관측 API가 없는 기본 경로. 새 상세 GET 응답으로 보충 |
| control-plane main | b8a56a3 | 충돌 없이 병합. 기존 DB 역방향 CDC 변경과 함께 API 회귀 검사 통과 |
| control-plane release/2 | 6a4b008 | 충돌 없이 병합. 기존 관측 저장소·OTEL·선택 리소스·전환 엔진 검사 통과 |
| control-plane 관측 PR #2 | c70fa25 | merge-tree에서 충돌 없음. 기존 관측 경로를 우선하는 동일한 호환 분기 |
| control-plane 온프레미스 PR #5 | 7775026 | merge-tree에서 충돌 없음. 프로비저닝 코드 변경 없음 |
| platform-frontend develop | fbd3cb8 | 기본 develop 및 release/2 합본의 정상·조회 실패·완료 응답 총 6개를 실제 Zod 계약으로 검증하고 구성도 렌더링 |
| redis-migrator release/2 | e5eaa20 | Proxy/Worker 상세 관측 필드 존재. 원본 관측·연결 이름 테스트 3개 및 subtest 2개 통과 |
| redis-migrator develop | 1278a46 | 기본 경로·드레인 정보는 제공하지만 details.redis.sync/proxy는 없음. 누락은 미보고로 남기며 상세 기능에는 release/2 수준의 모듈이 필요 |
| db-migration-adaptor main | f4d41f6 | 공통 응답 필드 대조. observationAvailable=false를 UNAVAILABLE로 유지 |
| object-migration main | 75da4f0 | 공통 응답 필드 대조. 제공되지 않는 값은 생성하지 않음 |

## 발견해 수정한 충돌

원래 변경은 `_detail`과 `_resource`를 교체해서 main·관측·release/2 변경과 충돌했다. 이제 GET의 표시 전용 경로에 한정하고 기존 도우미·POST 결과를 유지한다. 확장 응답은 `ObservedResourceView`와 `SerializeAsAny`로 전달해 팀원의 기본 ResourceView 필드를 중복 선언하거나 직렬화에서 없애지 않는다.

별도 관측 경로에서는 원본 리소스의 필드와 OTEL 출처·lastSuccessAt을 유지한 채 표시용 streams만 보충한다. 관측 저장소와 모듈 클라이언트를 변경하지 않는 것을 합본 테스트에서 확인했다. 공개 필드 목록도 팀원의 release/2 계약을 재사용한다.

release/2 합본과 프론트 계약을 함께 검사하며 복제 정보의 `reverse: null`, `initialCopy: null`이 프론트의 선택적 객체 계약과 맞지 않는 것도 확인했다. 이를 미제공 객체의 생략으로 정규화한다. 유효한 관측값·명시적인 0·false는 유지하고 전체 복사량 등을 추정하지 않는다. 별도 관측 경로의 출처와 오류 상태는 그대로 유지한다.

## 검증 범위와 남은 한계

main 합본은 전체 회귀 검사 221개와 후속 API 검사 55개가 통과했다. release/2 합본은 관측·OTEL·API·엔진·DB·선택 리소스의 핵심 검사를 실행했다. 직접 조회 전용 검사 4개는 이미 독립 관측 경로가 있는 합본에서 적용 대상이 아니므로 건너뛴다. OTEL 스냅샷을 이용하는 별도 호환 검사는 실제 관측 저장소를 사용한다.

핵심 합본 검사 130개가 통과했고, 미제공 객체 정규화 후 API·관측·호환 검사 65개도 통과했다. 프론트 계약·렌더링은 기본 응답과 release/2 합본 응답 각각 3개, 총 6개가 통과했다.

release/2의 배포 테스트는 원본과 합본 모두 동일한 19개 테스트가 실패하고 86개가 통과했다. 실패 테스트 ID가 같은 것을 JUnit 결과로 대조했다. Windows에서 POSIX 파일 권한(0600) 검사와 열린 SQLite 파일 정리가 실패한 경우다. 이번 PR로 새로 발생한 실패로 보고하지 않으며, Linux 배포 환경에서 전체 배포 검사를 통과했다고 주장하지 않는다.

DB·객체 저장소·Cloudflare·온프레미스에 실제 연결한 통합 이전은 실행하지 않았다. 브랜치 자동 병합과 API/회귀 테스트 통과는 실제 클라우드 운영 검증과 구별한다.
