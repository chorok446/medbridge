"""S3 호환 객체 저장소 접근. 버킷은 전부 비공개, 다운로드는 presigned URL만 사용."""

import uuid
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ErrorCode


def object_key(user_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """원본 파일명은 절대 키에 쓰지 않는다 (DB 메타데이터로만 보존)."""
    return f"users/{user_id}/documents/{document_id}/original.pdf"


def _make_client(settings: Settings, endpoint: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        config=Config(
            connect_timeout=settings.minio_timeout_seconds,
            read_timeout=settings.minio_timeout_seconds,
            retries={"max_attempts": 2},
            signature_version="s3v4",
        ),
    )


@lru_cache
def internal_client() -> Any:
    return _make_client(get_settings(), get_settings().minio_endpoint)


@lru_cache
def presign_client() -> Any:
    # presigned URL 서명에는 브라우저가 실제 접근할 호스트를 사용해야 한다
    return _make_client(get_settings(), get_settings().minio_public_endpoint)


def put_original(key: str, data: bytes) -> None:
    try:
        internal_client().put_object(
            Bucket=get_settings().minio_bucket_originals,
            Key=key,
            Body=data,
            ContentType="application/pdf",
        )
    except Exception as exc:
        raise AppError(
            ErrorCode.STORAGE_UPLOAD_FAILED,
            "파일 저장에 실패했습니다. 잠시 후 다시 시도해 주세요.",
            status_code=502,
            retryable=True,
        ) from exc


def get_original(key: str) -> bytes:
    resp = internal_client().get_object(Bucket=get_settings().minio_bucket_originals, Key=key)
    return resp["Body"].read()


def original_exists(key: str) -> bool:
    try:
        internal_client().head_object(Bucket=get_settings().minio_bucket_originals, Key=key)
        return True
    except Exception:
        return False


def delete_original(key: str) -> None:
    """실패 시 예외를 그대로 올린다 — 호출부가 보상 처리를 결정한다."""
    internal_client().delete_object(Bucket=get_settings().minio_bucket_originals, Key=key)


def presigned_original_url(key: str) -> str:
    return presign_client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": get_settings().minio_bucket_originals,
            "Key": key,
            "ResponseContentType": "application/pdf",
            "ResponseContentDisposition": "inline",
        },
        ExpiresIn=get_settings().presign_expiry_seconds,
    )
