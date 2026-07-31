"""bbox 검증·정규화. 좌표계: PyMuPDF 페이지 공간 (pt, 좌상단 원점, y 아래로 증가,
페이지 회전 적용 후 기준 — page.rect와 동일 공간). docs/pdf/coordinate-system.md 참조."""

from app.services.extraction.thresholds import BBOX_DECIMALS

BBox = tuple[float, float, float, float]


def clamp_bbox(bbox: BBox, width: float, height: float) -> BBox:
    """페이지 밖으로 나가는 좌표를 페이지 경계로 자르고, x0≤x1·y0≤y1을 보장한다."""
    x0, y0, x1, y1 = bbox
    x0, x1 = sorted((max(0.0, min(x0, width)), max(0.0, min(x1, width))))
    y0, y1 = sorted((max(0.0, min(y0, height)), max(0.0, min(y1, height))))
    return (
        round(x0, BBOX_DECIMALS),
        round(y0, BBOX_DECIMALS),
        round(x1, BBOX_DECIMALS),
        round(y1, BBOX_DECIMALS),
    )


def bbox_area(bbox: BBox) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def overlap_ratio(inner: BBox, outer: BBox) -> float:
    """inner가 outer와 겹치는 면적 / inner 면적."""
    ix0 = max(inner[0], outer[0])
    iy0 = max(inner[1], outer[1])
    ix1 = min(inner[2], outer[2])
    iy1 = min(inner[3], outer[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    area = bbox_area(inner)
    return inter / area if area > 0 else 0.0


def vertical_distance(a: BBox, b: BBox) -> float:
    """두 bbox의 세로 간격 (겹치면 0)."""
    if a[3] < b[1]:
        return b[1] - a[3]
    if b[3] < a[1]:
        return a[1] - b[3]
    return 0.0
