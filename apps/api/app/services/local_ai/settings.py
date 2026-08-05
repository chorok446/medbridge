"""로컬 AI(Ollama) 상수 — 주소·타임아웃·크기 상한·모델 카탈로그·안내 임계값.

Ollama 주소는 코드에 고정한다(사용자 입력 없음). IP literal이라 DNS 조회가 필요 없다.
"""

from dataclasses import dataclass

# 고정 loopback 주소 — 사용자에게 노출·입력시키지 않는다.
OLLAMA_BASE = "http://127.0.0.1:11434"
OLLAMA_OPENAI_BASE = OLLAMA_BASE + "/v1"

GIB = 1024**3

# --- 네트워크 타임아웃/크기 상한 ---
STATUS_TIMEOUT_SEC = 4.0  # /api/version, /api/tags — 짧게(감지)
STATUS_MAX_BYTES = 256 * 1024  # version/tags 응답 상한
TEST_TIMEOUT_SEC = 30.0  # 연결 테스트(모델 로드 포함될 수 있어 여유)
TEST_MAX_BYTES = 256 * 1024
PULL_CONNECT_TIMEOUT_SEC = 10.0
PULL_IDLE_TIMEOUT_SEC = 60.0  # 다운로드 청크 사이 무응답 상한
# 카탈로그 최대 모델(전문가형 19GiB)이 ~2Mbps 회선에서도 완주할 수 있는 여유.
# 멈춘 다운로드는 총 데드라인이 아니라 idle timeout이 잡는다.
PULL_TOTAL_DEADLINE_SEC = 24 * 60 * 60.0
PULL_MAX_LINE_BYTES = 64 * 1024  # NDJSON 한 줄 상한
# 진행 이벤트 '텍스트' 총량 상한(모델 바이트가 아님). Ollama는 다운로드 중 초당 ~2.5KB의
# 진행 줄을 보내므로 총 데드라인(24시간) 동안 최대 ~216MB가 정상 범위다. 목적은 무한
# 스트림 차단이지 느린 회선의 정상 다운로드를 끊는 것이 아니므로 그보다 넉넉하게 잡는다.
PULL_MAX_TOTAL_BYTES = 256 * 1024 * 1024

# Qwen3를 지원하는 Ollama 최소 버전(보수적 안내 기준). 미만이면 incompatible.
MIN_OLLAMA_VERSION = (0, 6, 0)

# 연결 테스트 시스템 프롬프트에 넣는 비사고 보조 지시(API 필드가 우선).
NO_THINK_HINT = "간결하게 한 문장으로만 답하세요. 사고 과정을 출력하지 마세요."


@dataclass(frozen=True)
class ModelSpec:
    model: str  # 내부 Ollama 모델명(allowlist)
    tier: str  # light | balanced | quality | advanced
    label: str  # 사용자 표시 이름
    description: str
    approx_bytes: int  # 표시·디스크 검사용 근사 크기


# 권장 모델 카탈로그 + allowlist. 이 목록 밖 모델명은 다운로드하지 않는다.
MODEL_CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec(
        model="qwen3:4b",
        tier="light",
        label="경량형",
        description="가장 가볍고 빠릅니다. 저사양 PC에 적합합니다.",
        approx_bytes=int(2.5 * GIB),
    ),
    ModelSpec(
        model="qwen3:8b",
        tier="balanced",
        label="균형형",
        description="속도와 품질의 균형입니다. 대부분의 PC에 권장합니다.",
        approx_bytes=int(5.2 * GIB),
    ),
    ModelSpec(
        model="qwen3:14b",
        tier="quality",
        label="고품질형",
        description="품질이 좋지만 메모리·저장 공간이 많이 필요합니다.",
        approx_bytes=int(9.3 * GIB),
    ),
    ModelSpec(
        model="qwen3:30b-a3b",
        tier="advanced",
        label="전문가형",
        description=(
            "가장 높은 품질입니다. 큰 용량에 비해 속도가 빠르지만 메모리 32GB 이상이 필요합니다."
        ),
        approx_bytes=int(19 * GIB),
    ),
)

ALLOWED_MODELS: frozenset[str] = frozenset(spec.model for spec in MODEL_CATALOG)
DEFAULT_MODEL = "qwen3:8b"  # 자동으로 14B를 고르지 않는다.

CATALOG_BY_MODEL: dict[str, ModelSpec] = {spec.model: spec for spec in MODEL_CATALOG}

# 다운로드에 요구하는 디스크 여유 = 근사 크기 + 안전 버퍼(다운로드·압축 해제 여유).
DISK_SAFETY_MARGIN_BYTES = 3 * GIB


def required_disk_bytes(spec: ModelSpec) -> int:
    return spec.approx_bytes + DISK_SAFETY_MARGIN_BYTES


# OS가 보고하는 물리 RAM은 하드웨어·커널 예약분 때문에 공칭 용량보다 항상 조금 작다
# (공칭 32GB 기기 → 보통 31.2~31.9GiB). 공칭 용량 기준으로 안내하려면 임계에 그 여유를
# 반영해야 한다 — 정확히 32GiB로 비교하면 문서가 권장하는 바로 그 기기가 경고를 받는다.
RAM_REPORTING_MARGIN = 0.94


def _reaches_nominal(gb: float, nominal_gb: int) -> bool:
    return gb >= nominal_gb * RAM_REPORTING_MARGIN


def ram_advice(model: str, total_ram_bytes: int | None) -> str:
    """RAM 안내 등급: recommended | selectable | warn | unknown.

    절대적 실행 가능 판정이 아니라 사용자 안내 기준이다(GPU VRAM만으로 판단하지 않음).
    임계(공칭 용량): ≤8GB 4B도 경고 / 12–23GB 4B 권장·8B 선택 / 24GB+ 8B 권장 /
    32GB+ 14B·30B-A3B 선택. 공칭 판정은 RAM_REPORTING_MARGIN이 보정한다.
    """
    if total_ram_bytes is None:
        return "unknown"
    gb = total_ram_bytes / GIB
    if model == "qwen3:4b":
        return "warn" if gb <= 8 else "selectable" if _reaches_nominal(gb, 24) else "recommended"
    if model == "qwen3:8b":
        if gb <= 8:
            return "warn"
        return "recommended" if _reaches_nominal(gb, 24) else "selectable"
    if model in ("qwen3:14b", "qwen3:30b-a3b"):
        # 30B-A3B는 MoE라 활성 파라미터는 3B지만 가중치 19GB가 통째로 올라간다 —
        # 둘 다 32GB급 메모리가 필요하므로 같은 기준을 쓴다.
        return "selectable" if _reaches_nominal(gb, 32) else "warn"
    return "unknown"
