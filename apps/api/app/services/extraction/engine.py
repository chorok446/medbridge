"""PyMuPDF 어댑터 — 페이지별 구조화 추출 (블록·줄·단어·이미지·표·크기·회전).

여기서는 좌표·텍스트를 가능한 한 원본 그대로 가져오고,
해석(읽기 순서·정규화·판정)은 상위 모듈이 담당한다.
"""

from dataclasses import dataclass, field

import pymupdf

from app.services.extraction.geometry import BBox, bbox_area, clamp_bbox

ENGINE_NAME = "pymupdf"
ENGINE_VERSION = pymupdf.__version__
SCHEMA_VERSION = 1  # 추출 스키마 변경 시 증가 (재처리 판단 기준)


@dataclass
class WordRec:
    bbox: BBox
    text: str
    block_index: int
    line_index: int
    word_index: int


@dataclass
class LineRec:
    bbox: BBox
    text: str
    line_index: int


@dataclass
class BlockRec:
    bbox: BBox
    text: str
    block_index: int
    block_type: str  # "text" | "image" | "vector"
    lines: list[LineRec] = field(default_factory=list)


@dataclass
class TableRec:
    bbox: BBox
    row_count: int
    column_count: int
    cells: list[list[str | None]]
    markdown: str
    status: str  # "extracted" | "failed"
    confidence: float


@dataclass
class PageData:
    page_number: int  # 1부터
    width: float
    height: float
    rotation: int
    raw_text: str
    blocks: list[BlockRec]
    words: list[WordRec]
    tables: list[TableRec]
    image_bboxes: list[BBox]
    image_area_ratio: float
    full_page_image: bool


def open_document(path: str) -> pymupdf.Document:
    return pymupdf.open(path)


def extract_page(doc: pymupdf.Document, index: int) -> PageData:
    """index는 0부터. 반환 page_number는 1부터."""
    page = doc[index]
    rect = page.rect
    width, height = float(rect.width), float(rect.height)
    rotation = int(page.rotation) % 360

    raw_text = page.get_text("text")

    # 블록·줄 (dict: 좌표 포함 구조화 결과)
    blocks: list[BlockRec] = []
    text_dict = page.get_text("dict")
    for block_index, raw_block in enumerate(text_dict.get("blocks", [])):
        bbox = clamp_bbox(tuple(raw_block["bbox"]), width, height)
        if raw_block.get("type") == 1:  # 이미지 블록
            blocks.append(
                BlockRec(bbox=bbox, text="", block_index=block_index, block_type="image")
            )
            continue
        lines: list[LineRec] = []
        line_texts: list[str] = []
        for line_index, raw_line in enumerate(raw_block.get("lines", [])):
            line_text = "".join(span.get("text", "") for span in raw_line.get("spans", []))
            lines.append(
                LineRec(
                    bbox=clamp_bbox(tuple(raw_line["bbox"]), width, height),
                    text=line_text,
                    line_index=line_index,
                )
            )
            line_texts.append(line_text)
        blocks.append(
            BlockRec(
                bbox=bbox,
                text="\n".join(line_texts),
                block_index=block_index,
                block_type="text",
                lines=lines,
            )
        )

    # 단어 (block/line 번호 포함)
    words = [
        WordRec(
            bbox=clamp_bbox((w[0], w[1], w[2], w[3]), width, height),
            text=w[4],
            block_index=int(w[5]),
            line_index=int(w[6]),
            word_index=int(w[7]),
        )
        for w in page.get_text("words")
    ]

    # 이미지 영역
    image_bboxes: list[BBox] = []
    covered = 0.0
    full_page_image = False
    page_area = max(width * height, 1.0)
    try:
        for info in page.get_image_info():
            bbox = clamp_bbox(tuple(info["bbox"]), width, height)
            image_bboxes.append(bbox)
            area = bbox_area(bbox)
            covered += area
            if area / page_area >= 0.85:
                full_page_image = True
    except Exception:
        pass  # 이미지 정보 실패는 페이지 실패로 이어지지 않는다
    image_area_ratio = min(1.0, covered / page_area)

    # 표 (실패해도 페이지 추출은 계속)
    tables: list[TableRec] = []
    try:
        finder = page.find_tables()
        for t in finder.tables:
            try:
                cells = t.extract()
                markdown = t.to_markdown()
                col_count = t.col_count if hasattr(t, "col_count") else (
                    max((len(r) for r in cells), default=0)
                )
                tables.append(
                    TableRec(
                        bbox=clamp_bbox(tuple(t.bbox), width, height),
                        row_count=len(cells),
                        column_count=col_count,
                        cells=cells,
                        markdown=markdown,
                        status="extracted",
                        confidence=0.7,  # 규칙 기반 추출 — 병합 셀·무테두리 표는 부정확할 수 있음
                    )
                )
            except Exception:
                tables.append(
                    TableRec(
                        bbox=clamp_bbox(tuple(t.bbox), width, height),
                        row_count=0,
                        column_count=0,
                        cells=[],
                        markdown="",
                        status="failed",
                        confidence=0.0,
                    )
                )
    except Exception:
        pass  # 표 탐지 실패는 페이지 실패로 이어지지 않는다

    return PageData(
        page_number=index + 1,
        width=width,
        height=height,
        rotation=rotation,
        raw_text=raw_text,
        blocks=blocks,
        words=words,
        tables=tables,
        image_bboxes=image_bboxes,
        image_area_ratio=image_area_ratio,
        full_page_image=full_page_image,
    )
