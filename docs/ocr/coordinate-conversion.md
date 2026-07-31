# OCR 좌표 변환

렌더링: `page.get_pixmap(dpi=D)` — **페이지 회전이 반영된** 시각 공간을 D dpi로 래스터화.
따라서 픽셀 공간과 Sprint 2의 저장 좌표계(시각 공간, pt)는 순수 스케일 관계다:

```text
pdf_pt = pixel × 72 / effective_dpi
```

- effective_dpi: 요청 DPI에서 최대 픽셀 수(40MP) 보호로 하향될 수 있음 (렌더 결과에 기록)
- 변환 후 `clamp_bbox`로 페이지 경계·x0≤x1·2자리 반올림 적용 (Sprint 2 정책 동일)
- 회전 페이지: 렌더와 저장 좌표 모두 회전 반영 공간이므로 추가 변환 불필요
- GUI 하이라이트: OCR 블록도 DocumentBlock으로 저장되므로 **기존 하이라이트 경로 그대로** 사용
  (확대·사용자 회전 변환 포함 — docs/pdf/coordinate-system.md)

픽셀 원본 좌표(pixel_x/y/width/height)도 OcrWord에 보존해 디버깅·재검증에 쓴다.
