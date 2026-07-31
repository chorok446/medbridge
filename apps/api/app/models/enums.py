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
    READY = "ready"  # 파일 검증 완료 (추출 대기)
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    PARTIALLY_EXTRACTED = "partially_extracted"
    OCR_REQUIRED = "ocr_required"
    EXTRACTION_FAILED = "extraction_failed"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"


class ProcessingStage(enum.StrEnum):
    UPLOAD = "upload"
    FILE_VALIDATION = "file_validation"
    EXTRACTION = "extraction"


class PageExtractionStatus(enum.StrEnum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    OCR_REQUIRED = "ocr_required"
    FAILED = "failed"


class ScanVerdict(enum.StrEnum):
    DIGITAL = "digital"
    MIXED = "mixed"
    SCANNED = "scanned"
    UNKNOWN = "unknown"


class BlockType(enum.StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VECTOR = "vector"
    TABLE = "table"
    CAPTION = "caption"
    UNKNOWN = "unknown"


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
    EXTRACT_DOCUMENT = "extract_document"
    OCR_DOCUMENT = "ocr_document"
    CHUNK_REBUILD = "chunk_rebuild"
    SUMMARIZE = "summarize"


class SummaryRunStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SummaryArtifactType(enum.StrEnum):
    OVERVIEW = "overview"
    SECTION_SUMMARY = "section_summary"
    KEY_CONCEPT = "key_concept"
    PREREQUISITE = "prerequisite"
    IMPORTANT_NUMBER = "important_number"
    TARGET_POPULATION = "target_population"
    LEARNER_EXPLANATION = "learner_explanation"
    STUDY_CAUTION = "study_caution"


class LearnerLevel(enum.StrEnum):
    CONCISE = "concise"
    NURSING_STUDENT = "nursing_student"
    EXPERIENCED_NURSE = "experienced_nurse"


class OcrRunStatus(enum.StrEnum):
    RUNNING = "running"
    OCR_COMPLETED = "ocr_completed"
    OCR_LOW_CONFIDENCE = "ocr_low_confidence"
    OCR_EMPTY = "ocr_empty"
    OCR_FAILED = "ocr_failed"
    OCR_CANCELLED = "ocr_cancelled"


class WordSource(enum.StrEnum):
    DIGITAL = "digital"
    OCR = "ocr"


class PageTextMethod(enum.StrEnum):
    """document_pages.extraction_method 의미 체계."""

    DIGITAL = "digital"
    OCR = "ocr"
    HYBRID = "hybrid"
    NONE = "none"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
