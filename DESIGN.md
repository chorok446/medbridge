---
name: MedBridge Study
description: 간호사의 밤 공부를 위한 로컬 PDF 학습 데스크톱 앱
colors:
  study-blue: "#2563eb"
  study-blue-deep: "#1d4ed8"
  info-tint: "#eff6ff"
  info-ink: "#1e40af"
  desk-bg: "#f8fafc"
  paper-surface: "#ffffff"
  pencil-line: "#e2e8f0"
  quiet-ink: "#64748b"
  reading-ink: "#475569"
  night-ink: "#0f172a"
  calm-green-tint: "#f0fdf4"
  calm-green-ink: "#166534"
  caution-amber-tint: "#fffbeb"
  caution-amber-ink: "#92400e"
  alert-red-tint: "#fef2f2"
  alert-red-ink: "#991b1b"
  alert-red-action: "#dc2626"
typography:
  headline:
    fontFamily: "Pretendard Variable, Pretendard, Apple SD Gothic Neo, Noto Sans KR, Malgun Gothic, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 700
    lineHeight: 1.3
  title:
    fontFamily: "Pretendard Variable, Pretendard, Apple SD Gothic Neo, Noto Sans KR, Malgun Gothic, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.4
  body:
    fontFamily: "Pretendard Variable, Pretendard, Apple SD Gothic Neo, Noto Sans KR, Malgun Gothic, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.625
  label:
    fontFamily: "Pretendard Variable, Pretendard, Apple SD Gothic Neo, Noto Sans KR, Malgun Gothic, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 500
    lineHeight: 1.4
rounded:
  sm: "4px"
  md: "6px"
  lg: "8px"
  full: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.study-blue}"
    textColor: "{colors.paper-surface}"
    rounded: "{rounded.md}"
    padding: "10px 20px"
  button-primary-hover:
    backgroundColor: "{colors.study-blue-deep}"
  button-secondary:
    backgroundColor: "{colors.paper-surface}"
    textColor: "{colors.night-ink}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  button-danger-outline:
    backgroundColor: "{colors.paper-surface}"
    textColor: "{colors.alert-red-ink}"
    rounded: "{rounded.sm}"
    padding: "6px 12px"
  card:
    backgroundColor: "{colors.paper-surface}"
    rounded: "{rounded.lg}"
    padding: "16px"
  badge-status:
    backgroundColor: "{colors.info-tint}"
    textColor: "{colors.info-ink}"
    rounded: "{rounded.full}"
    padding: "2px 8px"
---

# Design System: MedBridge Study

## 1. Overview

**Creative North Star: "밤의 공부방"**

퇴근한 간호사가 조용한 방에서 켜는 따뜻한 스탠드 조명 같은 화면. 이 시스템의 임무는 눈에 띄는 것이 아니라 **눈이 편한 것**이다. 밝은 회백색 책상(`desk-bg`) 위에 흰 종이(`paper-surface`)를 올려놓은 구성이 기본이고, 색은 상태를 전달할 때만 조용히 등장한다. 장식적 효과·그림자·움직임은 모두 절제되며, 유일하게 허용되는 강조는 "지금 눌러야 할 하나의 파란 버튼"이다.

PRODUCT.md의 anti-reference를 그대로 계승한다: 이 시스템은 **개발자 도구처럼 보여서는 안 되고**(로그·코드·기술 용어 노출 금지), **병원 EMR처럼 보여서도 안 된다**(빽빽한 표와 회색 관공서풍 금지). 컴포넌트의 손맛은 "크고 분명하게" — 큰 터치 영역, 넉넉한 패딩, 모호함 없는 라벨로, 컴퓨터가 익숙지 않은 사용자도 망설이지 않게 한다.

**Key Characteristics:**
- 흰 종이 + 회백색 책상 + 1px 연필선 테두리의 플랫 구성
- 단일 한국어 산세리프(Pretendard)만 사용, 위계는 크기·굵기로
- 상태 4색(파랑=진행/안내, 초록=안심, 호박=주의, 빨강=실패)은 항상 "연한 배경 틴트 + 진한 같은 계열 잉크" 쌍으로
- 모션은 상태 전달용 최소한(진행바 폭, 스피너)만

## 2. Colors

밝은 중립 바탕 위에서 파랑 하나가 행동을, 틴트 4쌍이 상태를 말하는 Restrained 전략이다.

### Primary
- **Study Blue** (#2563eb): 주 행동 버튼(PDF 파일 선택, 업로드, 지금 업데이트)과 링크, 진행바 채움. 화면당 한두 곳 이상 쓰지 않는다.
- **Study Blue Deep** (#1d4ed8): Study Blue의 hover 상태 전용.

### Neutral
- **Desk BG** (#f8fafc): 앱 전체 바탕. 순백보다 한 단계 가라앉힌 회백색으로 야간 눈부심을 줄인다.
- **Paper Surface** (#ffffff): 카드·패널·목록 행의 표면. 바탕과의 1단계 대비가 유일한 "높이"다.
- **Pencil Line** (#e2e8f0): 모든 테두리·구분선. 1px을 넘지 않는다.
- **Night Ink** (#0f172a): 제목과 본문의 기본 잉크.
- **Reading Ink** (#475569): 보조 설명문.
- **Quiet Ink** (#64748b): 메타 정보(파일 크기·날짜). 본문에는 쓰지 않는다.

### Tertiary (상태 틴트 4쌍)
- **Info Tint / Info Ink** (#eff6ff / #1e40af): 진행 중·안내 박스.
- **Calm Green Tint / Ink** (#f0fdf4 / #166534): 완료·안심 메시지.
- **Caution Amber Tint / Ink** (#fffbeb / #92400e): 주의·확인 요청(저품질 OCR, 업로드 금지 자료 안내).
- **Alert Red Tint / Ink / Action** (#fef2f2 / #991b1b / #dc2626): 실패 상태와 파괴적 행동. Red Action은 삭제·실패 재시도 버튼에만.

### Named Rules
**틴트-잉크 쌍 규칙.** 상태 색은 반드시 "같은 계열의 연한 배경 + 진한 텍스트" 쌍으로만 쓴다. 색 배경 위에 회색 텍스트는 금지 — 씻겨 보인다.

## 3. Typography

**Body Font:** Pretendard Variable (Apple SD Gothic Neo → Noto Sans KR → Malgun Gothic 폴백)

**Character:** 단일 가족, 4단 위계. 한국어 장문 가독을 위한 넉넉한 행간이 개성의 전부이며, 그것으로 충분하다.

### Hierarchy
- **Headline** (700, 1.5rem/24px, lh 1.3): 페이지 제목("내 학습자료"). 페이지당 하나.
- **Title** (600, 1.125rem/18px, lh 1.4): 카드 제목·문서명·섹션 헤더. 질문 탭에서는 사용자의 질문도 이 단계다.
- **Reading** (400, 1.0625rem/17px, lh 1.7): 사용자가 실제로 '공부하는' 글 — 답변 본문, 요약 본문, 추출 본문.
- **Body** (400, 0.875rem/14px, lh 1.625): UI 안내문·보조 설명 전용.
- **Label** (500, 0.75rem/12px, lh 1.4): 배지·메타·버튼 보조. 12px 미만은 금지.

### Named Rules
**야간 가독 규칙.** 공부하는 텍스트에는 Reading(17px/1.7)을 쓴다. 14px Body는 UI 안내문까지만 허용된다. PRODUCT.md: "40대 이상 사용자를 고려한 넉넉한 기본 글자 크기".

**한 줄 길이 규칙.** 읽기용 본문 컨테이너는 `max-width: 68ch`를 넘지 않는다. 한 줄이 너무 길면 눈이 다음 줄 첫머리를 놓쳐 장시간 학습에서 먼저 지친다.

## 4. Elevation

그림자를 쓰지 않는 완전 플랫 시스템이다. 깊이는 오직 두 가지로 표현한다: (1) Desk BG 위 Paper Surface의 명도 1단계 차이, (2) Pencil Line 1px 테두리. box-shadow 값은 코드베이스에 존재하지 않으며, 앞으로도 hover 강조는 배경 틴트 변화(`hover:bg-slate-50`)로만 한다.

### Named Rules
**종이 한 장 규칙.** 화면에는 책상과 그 위의 종이, 두 층만 존재한다. 종이 위에 또 떠 있는 종이(중첩 카드, 그림자 스택)는 금지.

**브랜드 마크 예외.** 그라디언트(Trust Blue `#2563eb` → Clinical Teal `#0D9488`)는 **앱 아이콘과 헤더 마크에만** 허용한다. UI 컴포넌트에는 여전히 금지다. `Clinical Teal`은 브랜드 색이며 상태 색·행동 색으로 쓰지 않는다 — 상태 4쌍과 Study Blue 규칙은 그대로다. 이 예외를 적어두지 않으면 "로고가 그라디언트니까 버튼도"로 번진다. 원본: `docs/brand/medbridge-logo-concept-1.svg`.

## 5. Components

### Buttons
- **Shape:** 부드러운 모서리 (6px; 작은 보조 버튼은 4px)
- **Primary:** Study Blue 배경 + 흰 글자, 넉넉한 패딩(10px 20px 이상). 화면의 "다음 할 일" 하나에만 부여.
- **Hover / Focus:** hover는 Study Blue Deep, focus는 `outline-2 outline-offset-2 outline-blue-600`. transition은 colors만.
- **Secondary:** 흰 배경 + Pencil Line 테두리 + Night Ink 글자, hover 시 Desk BG.
- **Danger:** 빨강은 두 단계 — 파괴 실행 버튼만 Red Action 채움, 그 외(삭제 진입, 실패 시 행동)는 빨강 테두리+잉크의 outline형.
- **Disabled:** opacity 40–50% + cursor-not-allowed. 비활성 이유는 인접 텍스트로 설명한다.

### Cards / Containers
- **Corner Style:** 8px
- **Background:** Paper Surface
- **Shadow Strategy:** 없음 (Elevation 참조)
- **Border:** Pencil Line 1px; 빈 상태(empty state)는 점선(dashed)
- **Internal Padding:** 16px

### Status Badge
- **Style:** rounded-full, 상태 틴트 배경 + 같은 계열 잉크, 12px Label
- **State:** 진행 중 상태는 `animate-pulse` 점(장식 아님, 활동 표시)과 퍼센트를 함께 표시. `motion-reduce` 대응 필수.

### Notice Boxes (안내·경고·오류 박스)
- **Style:** 틴트-잉크 쌍 규칙을 따르는 rounded(4px) 박스, px-3 py-2
- **State:** 오류는 `role="alert"` + 복구 버튼 동반 필수. 사용자가 스스로 취소한 결과는 오류 스타일로 표시하지 않는다.

### Inputs / Fields
- **Style:** 현재 파일 선택은 숨김 input + 버튼 트리거. 텍스트 입력이 필요해지면 Pencil Line 테두리 + 4px 라운드가 기준.
- **Focus:** 버튼과 동일한 blue outline.

### Navigation
- 상단 흰 헤더: 좌측 Study Blue 로고 텍스트, 우측 텍스트 링크 2개("내 학습자료", "앱 설정"). 활성 표시는 굵기.

### Progress (Signature)
- 2px~2.5px 높이의 rounded 트랙(Pencil Line보다 진한 slate-200) + Study Blue 채움 + `transition-all`. 반드시 `role="progressbar"` + aria 값 또는 인접 `aria-live` 텍스트를 동반한다.

## 6. Do's and Don'ts

### Do:
- **Do** 상태 색은 항상 틴트-잉크 쌍(#eff6ff+#1e40af 등)으로 쓰고, 오류 박스에는 반드시 다음 행동 버튼을 붙인다.
- **Do** 주 행동은 화면당 하나의 Study Blue 버튼으로 유지한다. 나머지는 Secondary.
- **Do** 모든 애니메이션에 `motion-reduce:` 변형을 제공한다 (PRODUCT.md: reduced-motion 존중).
- **Do** 읽기용 본문은 17px+ / 행간 1.7을 향해 키운다. UI 라벨 최소 12px.
- **Do** 내부 상태·실패 코드는 lib/format.ts의 STATUS_LABELS / FAILURE_GUIDES를 거쳐서만 화면에 낸다.

### Don't:
- **Don't** 개발자 도구처럼 보이게 하지 않는다 — 로그, 오류 코드, 포트/토큰/DPI/엔진명, 마크다운 원문(`| 파이프 |`)을 사용자에게 노출 금지 (PRODUCT.md anti-reference).
- **Don't** 병원 EMR처럼 보이게 하지 않는다 — 빽빽한 데이터 표, 회색 관공서풍 레이아웃 금지 (PRODUCT.md anti-reference).
- **Don't** box-shadow, 중첩 카드, 글래스모피즘, 그라디언트 텍스트, 1px 초과 색 사이드 스트라이프(border-left 액센트)를 쓰지 않는다.
- **Don't** 색 배경 위에 회색 글자를 올리지 않는다.
- **Don't** 10px 이하 텍스트를 만들지 않는다. `text-[10px]`는 발견 즉시 12px+로 교체.
- **Don't** 시스템 네이티브 confirm/prompt로 앱의 목소리를 끊지 않는다 — 확인은 앱 안의 다이얼로그로.
