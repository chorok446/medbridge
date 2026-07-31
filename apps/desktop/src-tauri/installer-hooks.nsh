; MedBridge NSIS 설치 훅 — sidecar 실행 파일 잠금으로 인한 덮어설치 실패 방지.
;
; 증상: 덮어설치 중 "medbridge-sidecar.exe 파일을 열 수 없습니다" 오류가 반복됨.
; 원인: Tauri가 기본 제공하는 CheckIfAppIsRunning(utils.nsh)은 메인 실행 파일
; (${MAINBINARYNAME}.exe = medbridge.exe)만 확인한다. 창을 닫아도 자식
; sidecar 프로세스(medbridge-sidecar.exe)가 남아있을 수 있는데, 그 경우
; 메인 exe 검사만으로는 걸러지지 않아 sidecar exe가 여전히 잠긴 채로
; 실제 파일 복사가 시작된다.
;
; 해결: 같은 검증된 매크로(currentUser 인식, 실행 중 확인→한국어 확인창→
; 종료, 실패 시 Abort)를 sidecar 실행 파일에도 적용한 뒤, 프로세스 종료가
; 실제 파일 잠금 해제로 이어졌는지 폴링으로 한 번 더 확인한다. 이 훅에서
; Abort하면 실제 File 복사 단계까지 가지 않으므로, 파일이 사용 중일 때
; 뜨는 NSIS의 중단/재시도/무시 대화상자를 만날 일이 없다 — "무시"로
; 넘어가 신버전 sidecar와 구버전 나머지 파일이 섞이는 부분 설치를 막는다.

!macro NSIS_HOOK_PREINSTALL
  ; nsis_tauri_utils::FindProcessCurrentUser / KillProcessCurrentUser 사용
  ; (${INSTALLMODE}가 currentUser이므로) — 관리자 권한이 필요 없다.
  !insertmacro CheckIfAppIsRunning "medbridge-sidecar.exe" "${PRODUCTNAME}"

  Call WaitForSidecarUnlock
!macroend

; 프로세스 종료 후 실제로 파일 잠금이 풀렸는지 폴링(최대 10초, 0.5초 간격).
; 실행 중인 exe는 쓰기 목적의 열기가 공유 위반으로 실패하므로, 그 시도가
; NSIS의 실제 File 복사 단계가 부딪히는 것과 동일한 조건을 미리 재현한다.
Function WaitForSidecarUnlock
  Push $0
  Push $1

  ; 최초 설치(파일이 아직 없음)는 대기할 대상이 없다.
  IfFileExists "$INSTDIR\medbridge-sidecar.exe" 0 wsu_done

  StrCpy $1 "0"

  wsu_check:
    ClearErrors
    FileOpen $0 "$INSTDIR\medbridge-sidecar.exe" a
    IfErrors wsu_locked wsu_unlocked

  wsu_locked:
    IntOp $1 $1 + 1
    IntCmp $1 20 wsu_timeout 0 0
    Sleep 500
    Goto wsu_check

  wsu_unlocked:
    FileClose $0
    Goto wsu_done

  wsu_timeout:
    MessageBox MB_OK|MB_ICONSTOP "MedBridge 관련 파일이 여전히 사용 중입니다.$\r$\nWindows를 재시작하거나 MedBridge를 완전히 종료한 뒤 설치를 다시 시도해 주세요."
    Abort "파일 잠금이 해제되지 않아 설치를 중단했습니다."

  wsu_done:
  Pop $1
  Pop $0
FunctionEnd
