# 출시 승인 artifact 형식

`scripts/check_release_gate.py`는 승인 파일의 문자열 판정만 신뢰하지 않는다. 실제 Qwen 평가
JSON, Windows 실기기 검증 JSON, 승인 파일이 동일한 코드·모델·installer를 가리키는지
교차검증한다. 하나라도 없거나 다르면 공개 릴리스를 차단한다.

## 공통 규칙

- `testedCommit`: 실제 평가와 실기기 검증을 수행한 코드의 **40자리 소문자 Git SHA**.
  `HEAD`, 축약 SHA, 대문자 SHA는 허용하지 않는다.
- `modelDigest`: Ollama 설치 목록에서 확인한 정확한 `sha256:<64자리 소문자 hex>` 값.
- `installerSha256`: Windows에서 36개 항목을 검증할 때 설치한 NSIS `.exe`의 SHA-256.
- artifact 파일을 완성한 뒤 파일 자체의 SHA-256을 계산해 승인 파일 `artifacts`에 기록한다.
- Qwen 평가, Windows 검증, 승인 파일의 commit·model digest·installer hash가 모두 일치해야 한다.
- Qwen 평가는 실제 `local` provider, `qwen3:8b`, 전체 case 각각 3회 이상이어야 한다.
- Windows 검증은 36개 항목 모두 통과해야 한다.

Windows installer 해시는 다음처럼 계산한다.

```powershell
Get-FileHash -Algorithm SHA256 .\MedBridge_Study_*_x64-setup.exe
```

## Qwen 평가 JSON

`evaluate_local_qa.py`가 만든 JSON에 실행 시점 provenance를 함께 보존한다. 기존 `summary`,
`gate`, `cases`는 그대로 두고 아래 최상위 필드가 반드시 있어야 한다.

```json
{
  "artifactType": "qwen3_8b_evaluation",
  "schemaVersion": 1,
  "testedCommit": "0123456789abcdef0123456789abcdef01234567",
  "provider": "local",
  "model": "qwen3:8b",
  "modelDigest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "gate": {
    "verdict": "default_recommended",
    "safetyPassed": true,
    "explicitSafetyPassed": true,
    "criticalCasesPassed": true,
    "modelGatePassed": true
  },
  "cases": [
    {"caseId": "예시", "runs": 3}
  ]
}
```

## Windows 실기기 검증 JSON

```json
{
  "artifactType": "windows_validation",
  "schemaVersion": 1,
  "testedCommit": "0123456789abcdef0123456789abcdef01234567",
  "status": "passed",
  "model": "qwen3:8b",
  "modelDigest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "installerSha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "checklist": {
    "total": 36,
    "passed": 36,
    "failed": 0,
    "blocked": 0
  }
}
```

## release-approval.json

아래 예시 값은 형식 설명용이며 실제 승인으로 사용할 수 없다.

```json
{
  "testedCommit": "0123456789abcdef0123456789abcdef01234567",
  "qwen3_8b": {
    "verdict": "default_recommended",
    "evalArtifact": "docs/testing/qa-eval-qwen3-8b.json",
    "modelDigest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "windowsValidation": {
    "status": "passed",
    "validationArtifact": "docs/testing/windows-release-validation.json",
    "installerSha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "artifacts": [
    {
      "kind": "qwen3_8b_evaluation",
      "path": "docs/testing/qa-eval-qwen3-8b.json",
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    },
    {
      "kind": "windows_validation",
      "path": "docs/testing/windows-release-validation.json",
      "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    }
  ]
}
```

워크플로는 빌드 후 생성된 NSIS installer의 SHA-256도 다시 확인한다. 실기기에서 검증한
installer와 재빌드 결과가 다르면 공개 게시하지 않는다. 따라서 실제로 검증한 바이너리를
승격하거나, 바이트 단위로 동일한 재현 빌드를 확보해야 한다.
