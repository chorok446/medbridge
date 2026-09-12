"""요약 모델 API 키의 OS credential storage 접근 (keyring).

키는 평문 DB·로그에 절대 저장하지 않는다. API 응답으로도 값을 반환하지 않고
"설정됨/미설정" 여부만 노출한다. 테스트·CI는 in-memory 백엔드를 주입해 실제 OS
저장소를 건드리지 않는다.

모델 호출은 기본 asyncio executor에서 돈다. 그 worker가 전송 직전 async guard를
기다리는 동안 guard가 다시 같은 executor로 keyring을 읽으면 모든 worker가 서로를
기다리는 교착이 생길 수 있다. 그래서 credential I/O는 전용 단일 executor로 격리한다.
"""

import asyncio
import functools
import hashlib
import hmac
import secrets as stdlib_secrets
from concurrent.futures import ThreadPoolExecutor

from app.core.config import get_settings

_KEY_USERNAME = "summary_api_key"
_IDENTITY_KEY_USERNAME = "summary_provider_identity_key"

# Windows Credential Manager 접근은 짧지만 동기 RPC다. 직렬화하면 같은 credential을
# 동시에 생성·갱신하는 경쟁도 피하고, 모델 worker가 기본 executor를 포화시켜도 읽힌다.
_KEYRING_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="summary-keyring")


def _service_name() -> str:
    return f"{get_settings().keyring_service_name}.summary"


def set_api_key(value: str) -> None:
    import keyring

    keyring.set_password(_service_name(), _KEY_USERNAME, value)


def get_api_key() -> str | None:
    import keyring

    return keyring.get_password(_service_name(), _KEY_USERNAME)


def get_api_key_with_identity() -> tuple[str | None, str | None]:
    """API 키와 DB에 저장해도 비밀이 아닌 설치별 identity digest를 함께 읽는다.

    API 키 자체나 무염 SHA-256을 input hash에 넣지 않는다. keyring에만 보관하는 무작위
    설치 키로 HMAC을 계산하므로 DB만 탈취한 공격자는 후보 키를 대입해 확인할 수 없다.
    API 키가 바뀌면 digest도 바뀌어 이전 provider checkpoint를 재사용하지 않는다.

    identity 키가 사라졌으면 새로 만든다. 이 경우 기존 checkpoint가 무효화될 뿐이며,
    서로 다른 provider 결과를 섞는 안전 문제는 생기지 않는다.
    """
    import keyring

    api_key = get_api_key()
    if not api_key:
        return api_key, None

    service = _service_name()
    identity_key = keyring.get_password(service, _IDENTITY_KEY_USERNAME)
    try:
        identity_bytes = bytes.fromhex(identity_key or "")
    except ValueError:
        identity_bytes = b""
    if len(identity_bytes) != 32:
        identity_bytes = stdlib_secrets.token_bytes(32)
        keyring.set_password(service, _IDENTITY_KEY_USERNAME, identity_bytes.hex())

    digest = hmac.new(identity_bytes, api_key.encode("utf-8"), hashlib.sha256).hexdigest()
    return api_key, digest


async def run_in_keyring_thread(call, /, *args):
    """keyring 동기 RPC를 모델 worker와 분리된 executor에서 실행한다."""
    loop = asyncio.get_running_loop()
    bound = functools.partial(call, *args)
    return await loop.run_in_executor(_KEYRING_EXECUTOR, bound)


def delete_api_key() -> None:
    import keyring
    import keyring.errors

    try:
        keyring.delete_password(_service_name(), _KEY_USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass  # 없으면 무시 (idempotent)


def has_api_key() -> bool:
    return bool(get_api_key())


def use_in_memory_backend() -> None:
    """테스트 전용 — 실제 OS 저장소 대신 프로세스 메모리에 키를 둔다."""
    import keyring
    from keyring.backend import KeyringBackend

    class _InMemoryKeyring(KeyringBackend):
        priority = 1  # type: ignore[assignment]

        def __init__(self) -> None:
            super().__init__()
            self._store: dict[tuple[str, str], str] = {}

        def get_password(self, service, username):
            return self._store.get((service, username))

        def set_password(self, service, username, password):
            self._store[(service, username)] = password

        def delete_password(self, service, username):
            self._store.pop((service, username), None)

    keyring.set_keyring(_InMemoryKeyring())
