"""세션 쿠키 기반 인증. Sprint 0 범위의 정식 이메일+비밀번호 인증."""

import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import get_settings

SESSION_COOKIE = "medbridge_session"

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def _serializer() -> URLSafeTimedSerializer:
    secret = get_settings().secret_key
    if not secret:
        # single_user 모드에서는 세션을 발급하지 않는다 (multi_user 전환 시 SECRET_KEY 필수)
        raise RuntimeError("SECRET_KEY is not configured")
    return URLSafeTimedSerializer(secret, salt="medbridge-session")


def create_session_token(user_id: uuid.UUID) -> str:
    return _serializer().dumps(str(user_id))


def read_session_token(token: str) -> uuid.UUID | None:
    if not get_settings().secret_key:
        return None
    try:
        raw = _serializer().loads(token, max_age=get_settings().session_max_age_seconds)
        return uuid.UUID(raw)
    except (BadSignature, SignatureExpired, ValueError):
        return None
