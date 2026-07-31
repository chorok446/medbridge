"""앱 데이터 경로 제공자. 사용자 데이터는 저장소·설치 디렉터리 밖(OS 앱 데이터)에 둔다.

Tauri가 MEDBRIDGE_APP_DATA_DIR 환경 변수로 경로를 주입하면 그 경로를 쓰고
(TauriAppDataPathProvider 역할), 없으면 OS 표준 앱 데이터 경로를 쓴다.
"""

from pathlib import Path
from typing import Protocol

from app.core.config import get_settings


class AppDataPathProvider(Protocol):
    @property
    def root(self) -> Path: ...
    @property
    def db_path(self) -> Path: ...
    @property
    def documents_dir(self) -> Path: ...
    @property
    def backups_dir(self) -> Path: ...
    @property
    def logs_dir(self) -> Path: ...
    @property
    def cache_dir(self) -> Path: ...


class DefaultAppDataPathProvider:
    """설정된 앱 데이터 루트 기준의 표준 하위 구조."""

    @property
    def root(self) -> Path:
        return get_settings().app_data_dir

    @property
    def db_path(self) -> Path:
        return self.root / "medbridge.db"

    @property
    def documents_dir(self) -> Path:
        return self.root / "documents"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    def ensure_directories(self) -> None:
        for d in (self.root, self.documents_dir, self.backups_dir, self.logs_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)


def get_path_provider() -> DefaultAppDataPathProvider:
    return DefaultAppDataPathProvider()
