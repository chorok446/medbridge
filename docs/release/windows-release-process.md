# Windows 릴리스 절차

## 워크플로 2개

| 워크플로 | 트리거 | 하는 일 |
|---|---|---|
| `ci.yml` (검증) | develop push, develop/main PR, 수동 | 버전 일치 검증 → 백엔드·프론트 테스트 → Windows에서 sidecar.exe 빌드+스모크 테스트 → NSIS 빌드 → **artifact 업로드** (Release 없음) |
| `release.yml` (릴리스) | main push, 수동 | 버전 게이트 통과 시: 전체 검증 → sidecar.exe → NSIS+updater 서명 → latest.json → `medbridge-releases` 공개 저장소에 Release 게시 |

## 릴리스 게이트

1. 다섯 파일 버전 일치 (`scripts/check-versions.py`) — web/desktop package.json,
   tauri.conf.json, api `__init__.py`, api pyproject
2. updater 서명 secret 존재 확인 (없으면 실패)
3. 버전 == 최신 릴리스 → **skip** (실패 아님, 로그에 명시)
4. 버전 < 최신 릴리스 → 실패
5. 같은 태그 Release 존재 → 실패 (덮어쓰기 금지)

즉, 새 릴리스를 내려면 버전 5곳을 올리고 main에 병합하면 된다.

## 필요한 GitHub Secrets

| Secret | 용도 | 생성 방법 |
|---|---|---|
| `TAURI_SIGNING_PRIVATE_KEY` | updater 서명 개인키 | `pnpm tauri signer generate -w medbridge.key` |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | 키 암호 | 생성 시 지정 |
| `TAURI_UPDATER_PUBKEY` | 공개키 (빌드 시 conf 주입) | 생성 시 함께 출력 |
| `RELEASES_REPO_TOKEN` | 공개 릴리스 저장소 게시용 PAT | `medbridge-releases` 저장소 한정, contents:write 최소 권한 |

개인키 취급: 저장소·소스·conf·.env.example·artifact·로그 어디에도 넣지 않는다.
안전한 백업: 키 파일과 암호를 비밀번호 관리자 등 오프라인 저장소에 별도 보관
(분실 시 기존 설치본이 업데이트를 받지 못하므로 새 키로 재배포 필요).

## 공개 저장소 (`chorok446/medbridge-releases`)

게시 자산: setup.exe(=updater 패키지), .sig, latest.json, 릴리스 노트.
소스·PDF·DB·로그·API 키·환경변수·서명 개인키는 절대 게시하지 않는다.
앱에는 GitHub 토큰을 포함하지 않는다 (public 저장소라 다운로드에 토큰 불필요).

## Windows Authenticode 코드 서명 (미적용)

- unsigned installer는 SmartScreen 경고가 뜬다 → 사용자 문서에 "추가 정보 → 실행" 안내
- 초기 내부 테스트에서만 unsigned 허용, 장기 실사용 전 도입 권장
  (이유: 경고 제거, 신뢰 확보, 백신 오탐 감소)
- 도입 위치: `release.yml`의 "Tauri build" 단계에 인증서/원격 서명 자격증명을
  Secrets로 추가하고 tauri.conf `bundle.windows.certificateThumbprint` 또는
  signCommand를 구성. updater 서명과는 별개다.
