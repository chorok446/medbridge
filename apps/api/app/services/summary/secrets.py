"""요약 모델 API 키의 OS credential storage 접근 (keyring).

키는 평문 DB·로그에 절대 저장하지 않는다. API 응답으로도 값을 반환하지 않고
"설정됨/미설정" 여부만 노출한다. 테스트·CI는 in-memory 백엔드를 주입해 실제 OS
저장소를 건드리지 않는다.
"""

from app.core.config import get_settings

_KEY_USERNAME = "summary_api_key"


def _service_name() -> str:
    return f"{get_settings().keyring_service_name}.summary"


def set_api_key(value: str) -> None:
    import keyring

    keyring.set_password(_service_name(), _KEY_USERNAME, value)


def get_api_key() -> str | None:
    import keyring

    return keyring.get_password(_service_name(), _KEY_USERNAME)


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
