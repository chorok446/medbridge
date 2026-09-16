# Windows 대형 PDF 크기 경계 벤치마크

> - 실행일: 2026-08-25 (KST)
> - testedCommit: `849fcd2913f699686445ff19160e3b35179b1673`
> - 런타임: `source-python-uvicorn`
> - Same-content size-envelope: 완료
> - 서로 다른 실제 대형 문서 soak: `HOLD`
> - 성능 SLA: 사전 기준이 없어 판정하지 않음
> - 공개 릴리스: `HOLD`

## 1. 범위와 주장 한계

이 측정은 동일한 2,173쪽 PDF 콘텐츠에 PDF comment padding을 추가해 정확히
300MiB, 500MiB, 800MiB로 만든 fixture를 사용했다. 따라서 다음 경로를 검증한다.

- 크기별 raw upload, staging, 증분 SHA-256, `fsync`, 원본 승격
- 800MiB 허용 경계까지의 파일 검증과 동일 콘텐츠 전체 추출
- 이 PC에서 관찰한 working set과 DB/WAL 증가량
- 정상 재기동과 추출 중단 뒤 동일 작업 자동 복구

다음 내용은 이 결과로 검증하지 않는다.

- 서로 다른 500MiB/800MiB 문서의 image, object, font, OCR 복잡도
- 더 많은 페이지나 OCR-heavy 문서의 처리 시간과 메모리 상한
- 설치 패키지에 포함된 sidecar의 메모리·종료 특성
- 모든 일반 Windows PC에서의 안정성 또는 성능 SLA
- Qwen 품질 gate, Windows GUI 36항목, 공개 릴리스 승인

세 fixture의 peak working set이 약 2.25GiB로 거의 같은 것은 comment padding보다
동일한 페이지/object 추출 작업이 메모리를 지배한다는 근거다. 실제 콘텐츠가 서로 다른
대형 문서 soak를 대신하는 근거로 사용해서는 안 된다.

## 2. 실행 provenance

| 항목 | 값 |
|---|---|
| OS | Windows 11 `10.0.26200.9168`, AMD64 |
| CPU | AMD Ryzen 5 9600X, 6 cores / 12 threads |
| RAM | 31.11GiB |
| 실행 시작 가용 RAM | 300/500/800MiB 순서로 4.29/4.70/4.54GiB |
| GPU | NVIDIA GeForce RTX 3080 10GB, driver 610.88 |
| 모델 부하 | `ollama ps`가 비어 있는 model-unloaded 상태 |
| 샘플 간격 | 100ms |
| 런타임 | 저장소 venv의 Python으로 실행한 source Uvicorn |
| Python 실행 파일 | 270,104 bytes |
| Python SHA-256 | `41123879d2e991377997acadf5f0cf8f34fa10d86362eded8c69b6041e2893b5` |
| DB revision | Alembic `0015` |

이 측정은 최종 installer의 packaged sidecar가 아니라 source runtime을 사용했다. 따라서
업로드·추출 코드의 크기 경계 근거로는 사용하지만 Windows 설치본의 메모리 또는 수명주기
36항목을 통과한 것으로 처리하지 않는다.

## 3. Fixture 계보

원본은 312,729,344 bytes, 2,173쪽의 실제 PDF이며 SHA-256은
`026c0df71869620cc7d6d88e1952b75683c9b562f37543441d44d0a9ab696ea3`이다.
최종 `startxref` 직전에 PDF comment padding만 넣어 콘텐츠 object graph와 페이지 수를
유지했다. fixture manifest SHA-256은
`3e8116ed30ee652250f4e388e169fc12879e6995d653e31840cc264944817fbb`이다.

| Fixture | 정확한 bytes | 페이지 | SHA-256 |
|---|---:|---:|---|
| 300MiB | 314,572,800 | 2,173 | `315d5f282346a05807612259d8bb7004a3ca2936ab9a47aafdfeac883523e010` |
| 500MiB | 524,288,000 | 2,173 | `3514f4189ce813f5285314bdafd9d6c960b603d052239ed26b4ace8643f46dc6` |
| 800MiB | 838,860,800 | 2,173 | `c45de227f8ad1fe975795ce327b8852bddd75f9e72677877f70db39fa05676cc` |

## 4. 유효성 조건

각 실행은 다음을 모두 만족할 때만 유효한 결과로 채택했다.

- exact testedCommit과 런타임 실행 파일의 크기·SHA-256 기록
- HTTP 201 및 저장 원본의 size·SHA-256 일치
- validation/extraction terminal 완료와 2,173개 고유 page row
- 2,171쪽 `EXTRACTED`, 2쪽 `OCR_REQUIRED`, 308,991 blocks
- 측정 sample error 0건과 실행 중 staging peak 관찰
- `quick_check=ok`, foreign key 위반 0건, 활성 job 0건
- 정상 재기동 후 `recovered_jobs=0`과 terminal 상태 보존

`OCR_REQUIRED` 두 쪽은 동일 콘텐츠의 세 실행에서 일관되며 추출 작업 자체는
`SUCCEEDED`, 문서는 `PARTIALLY_EXTRACTED`로 정상 종결됐다.

## 5. 처리 시간

| 크기 | HTTP | 업로드 | 검증 job | 추출 job | harness 처리 | E2E | terminal |
|---|---:|---:|---:|---:|---:|---:|---|
| 300MiB | 201 | 2.538s | 3.611s | 1,019.644s | 1,024.694s | 1,027.232s | `partially_extracted` |
| 500MiB | 201 | 3.973s | 3.731s | 912.218s | 916.108s | 920.081s | `partially_extracted` |
| 800MiB | 201 | 8.366s | 5.365s | 969.923s | 976.675s | 985.041s | `partially_extracted` |

추출 시간은 `EXTRACT_DOCUMENT.started_at`부터 `completed_at`까지이며, harness 처리
시간은 API polling을 포함한다. 실행 전에 합의된 성능 SLA가 없었으므로 이 수치를
SLA 통과로 판정하지 않는다.

## 6. 메모리와 저장소

| 크기 | 관찰 peak WS | OS peak WS | peak private | 종료 WS | staging peak |
|---|---:|---:|---:|---:|---:|
| 300MiB | 2,248.83MiB | 2,253.39MiB | 2,247.79MiB | 520.02MiB | 300MiB |
| 500MiB | 2,250.51MiB | 2,254.25MiB | 2,248.59MiB | 535.15MiB | 500MiB |
| 800MiB | 2,243.18MiB | 2,253.31MiB | 2,241.75MiB | 520.17MiB | 800MiB |

| 크기 | DB 증가 | WAL 증가 | DB+WAL 증가 | samples | sample errors |
|---|---:|---:|---:|---:|---:|
| 300MiB | 239.78MiB | 27.94MiB | 267.72MiB | 9,470 | 0 |
| 500MiB | 239.96MiB | 25.99MiB | 265.95MiB | 8,498 | 0 |
| 800MiB | 239.85MiB | 28.19MiB | 268.04MiB | 9,077 | 0 |

업로드 중 working set은 약 121MiB였지만 staging은 각 fixture의 정확한 전체 크기까지
증가했다. 이는 요청 본문 전체를 Python heap에 보유하지 않는 스트리밍 경로의 실측
근거다. 추출 종료 후 source process의 working set이 시작점보다 높았으므로 이 한 번의
실행만으로 장시간 반복 실행의 leak 부재를 주장하지 않는다.

source Uvicorn을 100ms polling으로 관찰하는 동안 응답은 정상 200이었지만 Windows
Proactor의 연결 종료 경고가 반복됐다. 추출 직후 `Ctrl+C`는 포트를 닫고 활성 job이
0이 된 뒤에도 일부 source process를 남겨, 실행 경로·PID·포트·DB 상태를 재확인한 후
그 정확한 process만 종료했다. 곧바로 수행한 짧은 재기동은 정상 종료됐다. 이는 source
harness 관찰이며 packaged sidecar의 종료 통과나 실패 판정으로 사용하지 않는다.

## 7. 재기동과 중단 복구

세 정상 실행은 각각 terminal 완료 뒤 같은 app-data root를 재기동했다. 모두
`recovered_jobs=0`이었고 문서 terminal, 페이지 수, attempt 1이 보존됐다.

별도의 300MiB 복구 fixture는 추출 중 프로세스를 종료한 상태에서 다음 값을 보존했다.

- 동일 문서와 동일 `EXTRACT_DOCUMENT` job
- attempt 1, `RUNNING`, 진행률 51%
- 고유 page row 1,123개, 범위 1..1,123
- blocks 106,914개

같은 app-data root를 재기동하자 startup recovery가 정확히 한 작업을 다시 큐에 넣었고,
같은 job이 attempt 2로 실행됐다. attempt 2는 2026-08-25 13:37:45 KST에 시작해
13:52:11 KST에 끝났으며 866.881초가 걸렸다. 최종 결과는 다음과 같다.

- 동일 문서 ID와 동일 job ID, attempt 2 `SUCCEEDED`
- 원본 314,572,800 bytes와 fixture SHA-256 일치
- 문서 `PARTIALLY_EXTRACTED`, 진행률 100%, page count 2,173
- page row 2,173개가 모두 고유하며 범위 1..2,173
- 2,171쪽 `EXTRACTED`, 2쪽 `OCR_REQUIRED`, blocks 308,991개
- Alembic `0015`, `quick_check=ok`, foreign key 위반 0건, 활성 job 0건

완료 후 같은 root를 다시 기동했을 때 `recovered_jobs=0`이었고 attempt 2와 terminal
결과가 유지됐다. 이 두 번째 확인 서버는 정상 종료됐다.

## 8. 원시 근거 무결성

원시 결과와 100ms sample log는 크기가 커 저장소에 커밋하지 않았다. 아래 SHA-256으로
검증 시점의 로컬 보존본을 식별한다.

| 크기 | 결과 JSON SHA-256 | sample log SHA-256 |
|---|---|---|
| 300MiB | `e3654bb70181782bdadd77c42dc16e0e97eccc8d790c45924003f11d450f9f51` | `9f0fafb6552e0ac8564132cd3840861238240f764aea4097133adebef5729c95` |
| 500MiB | `a47037647d1291fe0b6c658f982d8a8c9362cae0129490d13cec0daaca1a7ba8` | `a2984e8a01c07d5cc3f583bd265dcca7d8dd9c59c4ff8510ba2dd96f6e04dd37` |
| 800MiB | `071906f9f42b81d689badb5db098519d6def45dd2d371e441c5fb5d5fddbc5d7` | `ab94d77107b5ed98f6f7017885257f4f3ebb615ccc719b398fabc4dfdb45e55c` |

## 9. 남은 HOLD

Same-content 300/500/800MiB size-envelope와 중단 복구는 완료했다. 다음은 여전히
출시 차단 항목이다.

- 서로 다른 실제 300/500/800MiB PDF의 full extraction soak
- OCR-heavy, image/object/font 구조가 다른 문서의 peak memory와 장시간 반복 실행
- 최종 testedCommit의 packaged installer로 수행한 Windows GUI 36항목
- qwen3:8b repeat=3 품질 평가와 검증 artifact
- 실제 사용자 DB 사본의 migration 및 backup restore 훈련
- 보호된 `main`과 release Environment 승인

따라서 이 보고서만으로 `release-approval.json`을 생성하거나 공개 릴리스를 승인해서는
안 된다.
