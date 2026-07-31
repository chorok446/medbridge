# 좌표계 정책

## 저장 좌표

- **공간**: PyMuPDF 페이지 공간 = `page.rect` 기준
- **단위**: pt (1/72 inch)
- **원점**: 좌상단, y는 아래로 증가
- **회전**: 페이지 자체 회전(rotation 0/90/180/270)이 **적용된 후**의 공간
  (즉 화면에 보이는 방향 기준). `document_pages.rotation`에 원본 회전값 저장
- **정밀도**: 소수점 2자리 반올림 (`BBOX_DECIMALS`)
- **검증**: 저장 전 `clamp_bbox`가 x0≤x1·y0≤y1 보장 + 페이지 경계로 클램프

## 화면 변환 (GUI)

PDF.js 렌더링은 `getViewport({ scale, rotation: page.rotate })`를 사용한다.
이 viewport도 "회전 적용 후·좌상단 원점·y 아래" 공간이므로:

```text
screen = stored × scale                        (사용자 추가 회전 없음)
screen = rotateRect(stored, userRotate) × scale (사용자가 회전 버튼 사용 시)
```

`rotateRect`는 페이지 크기 기준 90° 단위 사각형 회전(`components/pdf-viewer.tsx`).
CSS 배율 단순 곱이 성립하는 이유는 양쪽 모두 페이지 회전을 이미 반영한 동일 공간이기
때문이며, 사용자 추가 회전만 별도 변환한다.

사용자 화면에는 좌표 숫자를 표시하지 않는다.
