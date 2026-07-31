from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """기동 시 검증되는 환경 변수 스키마. 누락·형식 오류 시 프로세스가 즉시 실패한다."""

    # 저장소 루트 .env(로컬 실행) 또는 컨테이너 주입 환경 변수를 읽는다
    model_config = SettingsConfigDict(env_file=("../../.env", ".env"), extra="ignore")

    app_env: str = Field(default="development", pattern="^(development|test|production)$")
    # single_user: 로컬 설치형(기본). multi_user: 향후 세션 인증 모드.
    app_mode: str = Field(default="single_user", pattern="^(single_user|multi_user)$")

    # 단일 사용자 모드의 로컬 사용자 (서버 측 설정 — 클라이언트 값은 신뢰하지 않는다)
    local_user_email: str = "user@example.com"
    local_user_display_name: str = "MedBridge User"

    # multi_user 모드에서만 필수 (세션 서명)
    secret_key: str | None = None

    database_url: str  # postgresql+asyncpg://...
    redis_url: str

    minio_endpoint: str  # API/worker에서 접근하는 내부 주소 (예: http://minio:9000)
    minio_public_endpoint: str  # 브라우저가 접근하는 주소 (presigned URL 서명 대상)
    minio_access_key: str
    minio_secret_key: str
    minio_bucket_originals: str = "medbridge-originals"
    minio_bucket_redacted: str = "medbridge-redacted"
    minio_bucket_derived: str = "medbridge-derived"
    minio_timeout_seconds: int = 10

    max_pdf_size_mb: int = 50
    presign_expiry_seconds: int = 300
    session_max_age_seconds: int = 7 * 24 * 3600

    @field_validator("database_url")
    @classmethod
    def _must_be_asyncpg(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use postgresql+asyncpg://")
        return v

    @model_validator(mode="after")
    def _validate_mode_requirements(self) -> "Settings":
        if self.app_mode == "multi_user":
            if not self.secret_key or len(self.secret_key) < 32:
                raise ValueError("multi_user 모드에서는 SECRET_KEY(32자 이상)가 필수입니다.")
        return self

    @property
    def max_upload_bytes(self) -> int:
        return self.max_pdf_size_mb * 1024 * 1024

    @property
    def database_url_sync(self) -> str:
        """Alembic·worker용 동기 드라이버 URL."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


@lru_cache
def get_settings() -> Settings:
    # 필수 값은 환경 변수/.env에서 로드된다
    return Settings()  # type: ignore[call-arg]
