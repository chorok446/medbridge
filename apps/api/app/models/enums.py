"""DB(PG ENUM)와 코드 양쪽에서 단일 정의로 관리하는 enum들.

마이그레이션은 이 모듈의 값과 반드시 일치해야 한다.
"""

import enum


class StudyLevel(enum.IntEnum):
    INTRO = 0
    BASIC = 1
    CLINICAL = 2
    EXPERT = 3


class ProcessingStatus(enum.StrEnum):
    CREATED = "created"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    QUEUED = "queued"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class ProcessingStage(enum.StrEnum):
    UPLOAD = "upload"
    FILE_VALIDATION = "file_validation"


class DocumentType(enum.StrEnum):
    # Sprint 1에서는 분류하지 않으므로 unknown만 사용. 값 목록은 명세 F-030 기준.
    LECTURE = "lecture"
    TEXTBOOK = "textbook"
    REVIEW = "review"
    SYSTEMATIC_REVIEW = "systematic_review"
    META_ANALYSIS = "meta_analysis"
    RCT = "rct"
    OBSERVATIONAL = "observational"
    CASE_REPORT = "case_report"
    GUIDELINE = "guideline"
    EXAM = "exam"
    PATIENT_CASE = "patient_case"
    OTHER = "other"
    UNKNOWN = "unknown"


class JobType(enum.StrEnum):
    VALIDATE_FILE = "validate_file"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
