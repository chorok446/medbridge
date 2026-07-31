from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """데스크톱 sidecar 설정. 모든 값에 안전한 기본이 있고, Tauri가 env로 주입한다.

    - MEDBRIDGE_APP_DATA_DIR: 앱 데이터 루트 (Tauri가 OS 앱 데이터 경로를 전달)
    - MEDBRIDGE_API_TOKEN: 로컬 API 접근 토큰 (Tauri가 생성·주입; 미설정 시 검사 생략)
    - DATABASE_URL: 테스트·개발용 override (기본: 앱 데이터 디렉터리의 medbridge.db)
    """

    model_config = SettingsConfigDict(env_file=("../../.env", ".env"), extra="ignore")

    app_env: str = Field(default="development", pattern="^(development|test|production)$")
    medbridge_app_data_dir: str | None = None
    medbridge_api_token: str | None = None

    database_url_override: str | None = Field(default=None, alias="DATABASE_URL")

    max_pdf_size_mb: int = 50
    max_concurrent_jobs: int = 2

    # 실제 상용 임베딩 공급자는 아직 없다 — "disabled"가 유일한 기본값이며,
    # 테스트에서만 "deterministic"으로 바꿔 쓴다. API 키를 여기 하드코딩하지 않는다.
    embedding_provider: str = Field(default="disabled", pattern="^(disabled|deterministic)$")

    @model_validator(mode="after")
    def _production_requires_token(self) -> "Settings":
        # 패키징 앱(production)에서 토큰이 없으면 인증 없이 열리므로 기동을 거부한다
        if self.app_env == "production" and not self.medbridge_api_token:
            raise ValueError("production 모드에서는 MEDBRIDGE_API_TOKEN이 필수입니다.")
        return self

    @property
    def max_upload_bytes(self) -> int:
        return self.max_pdf_size_mb * 1024 * 1024

    @property
    def app_data_dir(self) -> Path:
        if self.medbridge_app_data_dir:
            path = Path(self.medbridge_app_data_dir)
        else:
            import platformdirs

            path = Path(platformdirs.user_data_dir("MedBridge", appauthor=False))
        # SQLite 파일을 열기 전에 디렉터리가 반드시 존재해야 한다 (alembic 단독 실행 포함)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return f"sqlite+aiosqlite:///{self.app_data_dir / 'medbridge.db'}"

    @property
    def database_url_sync(self) -> str:
        """Alembic용 동기 드라이버 URL."""
        return self.database_url.replace("sqlite+aiosqlite://", "sqlite://", 1)


@lru_cache
def get_settings() -> Settings:
    # 필수 값은 환경 변수/.env에서 로드된다
    return Settings()  # type: ignore[call-arg]
