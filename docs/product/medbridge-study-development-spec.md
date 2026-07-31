# MedBridge Study 개발 기획서

## 0. 문서 목적

이 문서는 PDF·임상 케이스 기반 의학 학습 서비스 **MedBridge Study**의 1차 개발 기준을 정의한다.

포함 범위:

1. MVP 기능 명세
2. 화면별 UI·UX 요구사항
3. 시스템 아키텍처
4. 데이터베이스 스키마
5. API 명세
6. AI·RAG 처리 파이프라인
7. 안전·개인정보 보호 요구사항
8. 테스트 및 완료 기준
9. 단계별 개발 일정
10. Claude Code용 구현 지시문

본 서비스는 **의학 학습 보조 도구**이며 실제 환자의 진단, 처방, 응급 판단을 수행하지 않는다.

---

# 1. 제품 정의

## 1.1 한 줄 정의

사용자가 업로드한 의학 PDF와 케이스를 분석하여, 이해에 필요한 기초 개념부터 원문 해설, 최신 근거, 복습 문제까지 단계적으로 제공하는 개인 의학 학습 시스템.

## 1.2 핵심 사용자

- 의대생
- 간호학과·응급구조학과·보건계열 학생
- 의학 기초가 부족한 학습자
- 영어 논문과 강의자료를 함께 공부하려는 사용자
- 임상 케이스를 구조적으로 복습하려는 사용자

## 1.3 핵심 가치

1. 어려운 자료를 쉬운 순서로 다시 설명한다.
2. AI가 만든 설명과 원문 내용을 명확히 구분한다.
3. 주요 주장마다 PDF 페이지 또는 외부 근거를 연결한다.
4. 사용자가 모르는 선수지식을 자동으로 찾는다.
5. 학습 결과를 문제와 플래시카드로 전환한다.
6. 시간이 지나도 개인 지식 지도가 누적된다.

---

# 2. 개발 범위 결정

## 2.1 MVP v0.1 포함 범위

- 계정 또는 로컬 사용자 프로필
- PDF 업로드
- 스캔 PDF 여부 탐지
- 텍스트 및 문서 구조 추출
- 개인정보 후보 탐지 및 마스킹
- 학생 수준별 요약
- 전문용어 해설
- 선수지식 목록 생성
- PDF 원문 기반 질의응답
- 페이지 단위 출처 표시
- 문서별 노트
- 플래시카드 생성
- 기본 퀴즈 생성
- 학습 진행 상태 저장
- PubMed 기반 관련 논문 검색 베타
- 외부 근거의 검색일·출판일·연구 유형 표시
- “자료에 없음”, “근거 불충분” 상태 표시

## 2.2 MVP에서 제외

- 실제 환자 진단
- 처방 및 용량 결정
- 응급도 판정
- CT·MRI·병리 이미지 판독
- 의료기관 EMR 연동
- 여러 국가 가이드라인의 완전 자동 비교
- 유료 논문 전문 무단 수집
- 영상·음성 강의 처리
- 의료진용 임상 의사결정 지원
- 자동 논문 작성
- AI 단독 근거 등급 확정

## 2.3 단계별 제품 범위

### v0.1 문서 학습 MVP

PDF를 정확히 읽고, 쉬운 설명과 페이지 인용을 제공한다.

### v0.2 케이스 학습

케이스 구조화, 단계별 감별진단, 검사 선택, 해설을 제공한다.

### v0.3 최신 근거 강화

PICO, MeSH 검색어, 연구 유형 분류, 논문 비교표를 제공한다.

### v0.4 개인 지식 지도

취약 개념, 반복 학습, 문서 간 개념 연결을 제공한다.

---

# 3. 사용자 흐름

## 3.1 최초 사용자 흐름

1. 서비스 진입
2. 학습 수준 선택
3. 개인정보 및 의료 안전 고지 확인
4. PDF 업로드
5. 파일 분석 상태 확인
6. 문서 유형 확인
7. 선수지식 진단
8. 요약 및 원문 학습
9. 문서 기반 질문
10. 퀴즈 및 플래시카드 생성
11. 학습 완료 또는 복습 예약

## 3.2 재방문 사용자 흐름

1. 대시보드 진입
2. 오늘 복습할 개념 확인
3. 최근 문서 또는 케이스 재개
4. 취약 개념 복습
5. 새로운 관련 논문 확인
6. 퀴즈 재응시

## 3.3 문서 학습 흐름

1. PDF 업로드
2. 파일 검사
3. 텍스트 추출
4. 문서 섹션 분리
5. 표·그림·참고문헌 인식
6. 개인정보 후보 탐지
7. 사용자 확인 후 마스킹
8. 개념 추출
9. 선수지식 생성
10. 요약 생성
11. 주장별 출처 검증
12. 학습 화면 공개

---

# 4. 기능 명세

## F-001 사용자 프로필

### 목적

사용자의 기본 학습 수준과 관심 과목을 저장한다.

### 입력

- 표시 이름
- 학습 수준
- 관심 과목
- 한국어·영어 표시 선호
- 외부 AI 전송 허용 여부
- 학습자료 보관 정책

### 학습 수준

- Level 0: 의학 입문
- Level 1: 기초의학
- Level 2: 임상실습
- Level 3: 전공자

### 완료 조건

- 사용자가 언제든 학습 수준을 변경할 수 있다.
- 변경된 수준은 이후 생성되는 설명에 반영된다.
- 문서별로 전역 설정보다 다른 수준을 선택할 수 있다.

---

## F-010 PDF 업로드

### 지원 조건

- 확장자: PDF
- 파일 크기 제한: 환경 설정 가능
- 암호화 PDF: 업로드 거부 또는 암호 입력 요구
- 손상 파일: 명확한 오류 표시
- 텍스트 PDF와 스캔 PDF 구분

### 업로드 상태

- 대기
- 업로드 중
- 파일 검사 중
- 텍스트 추출 중
- 개인정보 검사 중
- AI 분석 중
- 검증 중
- 완료
- 일부 완료
- 실패

### 완료 조건

- 업로드 진행률을 표시한다.
- 실패 단계와 원인을 사용자에게 보여준다.
- 동일 파일의 중복 업로드를 탐지한다.
- 원본 해시를 저장한다.
- 파일 삭제 시 파생 데이터 삭제 옵션을 제공한다.

---

## F-011 파일 안전 검사

### 검사 항목

- MIME 유형
- 확장자와 실제 형식 일치 여부
- 파일 크기
- 암호화 여부
- 비정상 객체
- 악성 스크립트 포함 가능성
- 페이지 수
- 추출 가능한 텍스트 비율

### 완료 조건

- 서버에서 원본 파일명을 직접 저장 경로로 사용하지 않는다.
- 파일은 UUID 기반 이름으로 저장한다.
- 분석 전 안전 검사에 실패하면 파싱하지 않는다.

---

## F-020 개인정보 탐지

### 탐지 대상

- 이름
- 주민등록번호
- 전화번호
- 이메일
- 주소
- 생년월일
- 병원 등록번호
- 검사 식별번호
- 의료기관명
- 의료진명
- 날짜 조합으로 식별 가능성이 높은 정보

### 동작

1. 규칙 기반 탐지
2. NER 기반 탐지
3. 사용자 확인
4. 마스킹본 생성
5. 외부 모델에는 마스킹본만 전송

### 완료 조건

- 원본과 마스킹본을 구분하여 저장한다.
- 마스킹 위치를 사용자가 검토할 수 있다.
- 외부 AI 호출 로그에는 원문 개인정보를 남기지 않는다.
- 사용자가 마스킹 검토를 건너뛸 경우 경고한다.

---

## F-030 PDF 파싱

### 추출 항목

- 페이지별 텍스트
- 제목
- 저자
- 초록
- 섹션 제목
- 본문 문단
- 표 제목과 내용
- 그림 캡션
- 참고문헌
- DOI·PMID·PMCID 후보
- 페이지 좌표
- 문단 좌표

### 문서 유형 분류

- 강의자료
- 교과서
- 종설
- 체계적 문헌고찰
- 메타분석
- 무작위 임상시험
- 관찰연구
- 증례보고
- 임상 가이드라인
- 시험자료
- 환자 케이스
- 기타
- 불확실

### 완료 조건

- 모든 텍스트 조각은 원본 페이지와 연결된다.
- 결과와 고찰 섹션을 분리한다.
- 표 내용과 본문을 가능한 한 분리 저장한다.
- 추출 신뢰도가 낮은 페이지를 표시한다.
- OCR 결과는 OCR 사용 여부와 신뢰도를 표시한다.

---

## F-040 다단계 요약

### 출력 종류

#### 30초 요약

- 문서 주제
- 핵심 결론
- 기억할 내용 3개

#### 학생용 요약

- 필요한 기초 개념
- 주요 용어
- 정상 생리
- 병태생리
- 증상·검사 연결
- 임상적 의미
- 혼동하기 쉬운 내용

#### 논문 분석

- 연구 질문
- 연구 설계
- 대상자
- 중재
- 비교군
- 주요 평가변수
- 결과
- 제한점
- 적용 가능성

#### 시험 대비 요약

- 핵심 암기 포인트
- 자주 묻는 구분
- 오답 유도 포인트
- 예상 문제

### 완료 조건

- 핵심 문장마다 출처 상태가 존재한다.
- 업로드 문서에 없는 내용을 문서 내용처럼 표현하지 않는다.
- 결과 수치에는 단위와 대상 집단을 함께 표시한다.
- 사용자가 설명 수준을 변경할 수 있다.
- 요약 생성 실패 시 원문 열람 기능은 유지한다.

---

## F-050 선수지식 생성

### 출력

- 필수 선수지식
- 권장 선수지식
- 각 개념의 중요 이유
- 예상 학습 시간
- 미니 설명
- 확인 문제

### 사용자 상태

- 알고 있음
- 조금 알고 있음
- 잘 모름
- 처음 봄

### 완료 조건

- 선수지식은 현재 문서의 개념과 연결된다.
- 사용자가 “잘 모름”을 선택한 개념은 별도 학습 카드로 생성된다.
- 선수지식 학습 후 원래 문서 위치로 돌아갈 수 있다.

---

## F-060 출처 및 주장 검증

### 출처 유형

- 업로드 PDF
- 사용자 노트
- 외부 논문 초록
- 외부 공개 원문
- 공식 가이드라인
- AI 보조 설명

### 주장 상태

- 원문 직접 확인
- 원문에서 합리적으로 추론
- 외부 근거 확인
- 복수 근거 종합
- 근거 불충분
- 자료에 없음
- 검증 실패

### UI 표시

각 문장 또는 문단에 다음 정보를 표시한다.

- 출처 배지
- 문서명
- 페이지
- 섹션
- 원문 보기
- 외부 논문 식별자
- 검증 상태

### 완료 조건

- 출처 클릭 시 PDF 해당 페이지로 이동한다.
- 가능하면 원문 문단을 하이라이트한다.
- 검증 실패 문장은 기본 답변에서 제거한다.
- 상충하는 근거가 있으면 한쪽 결론으로 단정하지 않는다.

---

## F-070 문서 기반 질의응답

### 질문 예시

- 이 부분을 더 쉽게 설명해 줘.
- 왜 이 검사를 시행했어?
- 표 2가 의미하는 내용을 설명해 줘.
- 이 연구 결과가 실제로 중요한가?
- 이 내용과 관련된 기초 생리를 설명해 줘.
- 문서에서 이 약의 부작용을 언급했어?
- 이 문서와 최신 연구가 다른 부분은 무엇이야?

### 답변 구조

1. 직접 답변
2. 쉬운 설명
3. 임상적 의미
4. 한계 또는 주의점
5. 출처
6. 확인 문제 또는 다음 학습 항목

### 완료 조건

- 문서에 없는 정보는 “자료에 없음”으로 표시한다.
- 외부 근거 검색이 사용된 경우 이를 명확히 구분한다.
- 질문과 관련 없는 문서 조각을 과도하게 사용하지 않는다.
- 답변 기록을 문서별로 저장한다.

---

## F-080 플래시카드

### 카드 유형

- 용어 → 정의
- 정의 → 용어
- 병태생리 순서
- 검사 → 의미
- 약물 → 기전
- 질환 → 핵심 특징
- Cloze 빈칸

### 완료 조건

- 카드마다 출처를 저장한다.
- 사용자가 수정할 수 있다.
- 삭제·보관 상태를 지원한다.
- 정답 평가를 저장한다.
- 이후 간격 반복 시스템과 연결할 수 있는 구조로 만든다.

---

## F-081 퀴즈

### 문제 유형

- 단일 선택
- 복수 선택
- 단답형
- 순서 배열
- 참·거짓
- 케이스형

### 난이도

- 기초
- 중간
- 심화

### 오답 분석

- 용어 부족
- 정상 생리 부족
- 병태생리 혼동
- 검사 해석 오류
- 감별진단 오류
- 연구 방법론 오류
- 문제 해석 오류

### 완료 조건

- 정답 근거를 제공한다.
- 해설에 출처가 연결된다.
- 애매한 문제는 생성 단계에서 제외한다.
- 사용자가 문제 오류를 신고할 수 있다.

---

## F-090 최신 근거 검색 베타

### 검색 입력

- 질환
- 치료
- 검사
- 예후
- 위해성
- 문서에서 추출한 임상 질문

### 검색 처리

1. 질문 유형 판정
2. PICO 초안 생성
3. 핵심 영문 용어 생성
4. 동의어 및 MeSH 후보 생성
5. PubMed 검색
6. 중복 제거
7. 연구 유형 분류
8. 관련성 순위화
9. 출판일·연구 유형·대상자 표시
10. 업로드 자료와 비교

### 제한

- 최신 논문이 가장 높은 근거라고 단정하지 않는다.
- 초록만 확보된 논문은 초록 기반이라고 표시한다.
- 유료 원문을 자동으로 우회 수집하지 않는다.
- 철회·정정 여부를 가능한 범위에서 확인한다.
- AI가 근거 수준을 확정하는 것이 아니라 보조 평가를 제공한다.

### 완료 조건

- 검색식과 검색 실행일을 저장한다.
- 결과마다 출판일과 연구 유형을 표시한다.
- 업로드 자료의 주장과 일치·불일치를 구분한다.
- 근거가 부족하면 부족하다고 표시한다.

---

# 5. 화면별 UI·UX 요구사항

## 5.1 전체 레이아웃

### 데스크톱

- 왼쪽: 글로벌 내비게이션
- 가운데: 주요 작업 영역
- 오른쪽: 컨텍스트 패널
- 상단: 현재 문서, 검색, 학습 수준, 처리 상태

### 모바일

MVP에서는 문서 읽기와 간단한 복습만 지원한다.

- PDF와 해설을 탭으로 분리
- 복잡한 3단 패널은 제공하지 않음
- 업로드와 관리자 기능은 데스크톱 우선

---

## 5.2 대시보드

### 구성

- 최근 학습 문서
- 처리 중인 문서
- 오늘 복습
- 취약 개념
- 최근 생성한 플래시카드
- 새로 검색된 관련 근거
- 학습 연속 기록

### 주요 행동

- 새 문서 업로드
- 최근 문서 이어보기
- 오늘 복습 시작
- 취약 개념 보기

### 빈 상태

첫 방문 시 다음 행동을 안내한다.

1. PDF 업로드
2. 학습 수준 설정
3. 샘플 문서 체험

---

## 5.3 업로드 화면

### 필수 요소

- 드래그앤드롭 영역
- 파일 선택 버튼
- 지원 형식
- 개인정보 업로드 경고
- 외부 AI 전송 설정
- 파일별 진행 상태
- 오류 메시지

### 업로드 후 설정

- 문서 제목
- 과목
- 문서 유형
- 학습 수준
- 외부 근거 검색 여부
- 개인정보 검사 여부

### UX 원칙

- 분석이 완료되기 전에도 원문 미리보기 제공
- 처리 단계가 명확하게 보이도록 구성
- 실패한 단계만 재시도 가능

---

## 5.4 개인정보 검토 화면

### 구성

- 왼쪽: 원문
- 가운데: 탐지된 항목 목록
- 오른쪽: 마스킹 미리보기

### 행동

- 개인정보로 확정
- 오탐으로 제외
- 직접 영역 선택
- 전체 마스킹 적용
- 마스킹 후 분석 시작

### 경고

마스킹 검토 없이 외부 모델을 사용하는 경우 명확한 경고 모달을 표시한다.

---

## 5.5 문서 학습 화면

### 왼쪽 패널: PDF

- 페이지 이동
- 확대·축소
- 검색
- 문단 선택
- 하이라이트
- 원문 좌표 이동

### 중앙 패널: 학습 콘텐츠

탭:

- 핵심 요약
- 학생용 설명
- 논문 분석
- 시험 대비
- 질문 기록

### 오른쪽 패널

탭:

- 선수지식
- 용어
- 출처
- 관련 논문
- 노트

### 텍스트 선택 메뉴

- 쉽게 설명
- 더 깊게 설명
- 용어 분석
- 병태생리 연결
- 질문하기
- 플래시카드 만들기
- 노트 저장
- 출처 확인

### 출처 인터랙션

- 출처 클릭 시 원문 위치 이동
- 외부 논문은 상세 카드 열기
- 근거 불충분은 경고색 대신 명시적 텍스트 사용
- 출처 패널에서 원문 인용 범위를 확인

---

## 5.6 선수지식 화면

### 구성

각 개념 카드에 다음을 표시한다.

- 개념명
- 쉬운 정의
- 왜 필요한가
- 예상 학습 시간
- 현재 숙련도
- 관련 문서 위치

### 행동

- 알고 있음
- 조금 알고 있음
- 잘 모름
- 처음 봄
- 미니 수업 시작

---

## 5.7 질의응답 화면

### 구성

- 질문 입력
- 현재 문서만 검색
- 내 자료 전체 검색
- 외부 근거 포함
- 답변 수준 선택

### 답변 카드

- 핵심 답변
- 쉬운 설명
- 근거
- 불확실성
- 관련 개념
- 플래시카드 생성
- 답변 평가

### 답변 평가

- 도움이 됨
- 너무 어려움
- 출처가 맞지 않음
- 내용이 틀린 것 같음
- 더 자세히 필요

---

## 5.8 근거 탐색 화면

### 필터

- 연구 유형
- 출판연도
- 대상 환자군
- 전문 이용 가능 여부
- 리뷰·가이드라인 우선
- 최신순
- 관련성순

### 논문 카드

- 제목
- 저자
- 저널
- 발행일
- 연구 유형
- 대상자 수
- 핵심 결론
- 현재 질문과의 관련성
- 초록 기반 여부
- DOI·PMID
- 저장

### 비교 모드

최대 5개 논문을 표로 비교한다.

- 연구 질문
- 대상자
- 중재
- 비교군
- 결과
- 효과크기
- 제한점
- 적용 가능성

---

## 5.9 복습 화면

### 구성

- 오늘의 플래시카드
- 틀린 문제
- 취약 개념
- 문서별 진도
- 최근 학습 기록

### 완료 상태

- 미학습
- 학습 중
- 복습 필요
- 안정적 기억
- 숙달

---

# 6. 시스템 아키텍처

## 6.1 권장 구조

```text
Web Client
  |
API Gateway / Backend
  |
  +-- Authentication
  +-- Document Service
  +-- Study Service
  +-- Evidence Service
  +-- AI Orchestrator
  +-- Job Queue
  |
  +-- PostgreSQL
  +-- Vector Search
  +-- Object Storage
  +-- Redis
```

## 6.2 권장 기술

### Frontend

- Next.js App Router
- TypeScript
- React Query 또는 서버 상태 관리 계층
- PDF.js 기반 PDF 뷰어
- Tailwind CSS
- 접근 가능한 컴포넌트 라이브러리

### Backend

- Python
- FastAPI
- Pydantic
- SQLAlchemy
- Alembic
- PostgreSQL
- pgvector
- Redis
- Celery, Dramatiq 또는 동등한 작업 큐

### Storage

- 개발: 로컬 파일시스템 또는 MinIO
- 운영: S3 호환 객체 저장소
- 원본과 마스킹본을 별도 버킷 또는 경로로 분리

### 문서 처리

- PyMuPDF
- OCR 엔진
- GROBID 선택 연동
- 표 추출 모듈
- 참고문헌 파서

### AI

- 모델 공급자 추상화
- 임베딩 모델 추상화
- 작업별 모델 라우팅
- 로컬 모델과 외부 모델 선택 가능

---

# 7. 저장소 구조

```text
medbridge/
  apps/
    web/
    api/
    worker/
  packages/
    ui/
    shared-types/
    prompts/
    eslint-config/
  infra/
    docker/
    migrations/
  docs/
    product/
    architecture/
    api/
  tests/
    fixtures/
    e2e/
  .env.example
  docker-compose.yml
  README.md
```

## 7.1 Web 구조

```text
apps/web/
  app/
    dashboard/
    documents/
    evidence/
    review/
    settings/
  components/
    document-viewer/
    citation/
    study/
    evidence/
    privacy/
  lib/
    api/
    auth/
    validation/
  types/
```

## 7.2 API 구조

```text
apps/api/
  app/
    api/
      routes/
    core/
    db/
    models/
    schemas/
    services/
      documents/
      privacy/
      rag/
      evidence/
      study/
    prompts/
    providers/
    workers/
```

---

# 8. 데이터베이스 스키마

## 8.1 users

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| email | text | 로그인 이메일 |
| display_name | text | 표시 이름 |
| study_level | enum | 0~3 |
| preferred_language | text | 기본 언어 |
| external_ai_allowed | boolean | 외부 AI 허용 |
| created_at | timestamptz | 생성일 |
| updated_at | timestamptz | 수정일 |

## 8.2 documents

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| user_id | UUID | 소유자 |
| title | text | 문서 제목 |
| original_filename | text | 원본 파일명 |
| storage_key | text | 원본 저장 위치 |
| redacted_storage_key | text | 마스킹본 위치 |
| sha256 | text | 중복 탐지 |
| mime_type | text | 실제 MIME |
| file_size | bigint | 파일 크기 |
| page_count | integer | 페이지 수 |
| document_type | enum | 문서 유형 |
| language | text | 문서 언어 |
| processing_status | enum | 처리 상태 |
| extraction_confidence | numeric | 추출 신뢰도 |
| external_evidence_enabled | boolean | 외부 검색 사용 |
| created_at | timestamptz | 생성일 |
| updated_at | timestamptz | 수정일 |

인덱스:

- user_id, created_at
- sha256
- processing_status

## 8.3 document_pages

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| document_id | UUID | 문서 |
| page_number | integer | 1부터 시작 |
| raw_text | text | 원문 |
| normalized_text | text | 정규화 텍스트 |
| ocr_used | boolean | OCR 여부 |
| ocr_confidence | numeric | OCR 신뢰도 |
| width | numeric | 페이지 너비 |
| height | numeric | 페이지 높이 |

유니크:

- document_id + page_number

## 8.4 document_sections

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| document_id | UUID | 문서 |
| section_type | enum | abstract, methods 등 |
| heading | text | 제목 |
| content | text | 내용 |
| start_page | integer | 시작 페이지 |
| end_page | integer | 끝 페이지 |
| order_index | integer | 순서 |
| embedding | vector | 임베딩 |

## 8.5 document_chunks

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| document_id | UUID | 문서 |
| section_id | UUID | 섹션 |
| page_number | integer | 페이지 |
| content | text | 검색 단위 |
| bbox | jsonb | 원문 좌표 |
| token_count | integer | 토큰 수 |
| chunk_type | enum | paragraph, table 등 |
| embedding | vector | 임베딩 |
| extraction_confidence | numeric | 신뢰도 |

## 8.6 privacy_findings

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| document_id | UUID | 문서 |
| finding_type | enum | 이름, 전화번호 등 |
| original_text_encrypted | text | 암호화 값 |
| replacement | text | 마스킹 문자열 |
| page_number | integer | 페이지 |
| bbox | jsonb | 좌표 |
| confidence | numeric | 탐지 신뢰도 |
| status | enum | pending, confirmed, rejected |
| detected_by | text | rule, ner, user |

## 8.7 medical_concepts

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| canonical_name | text | 표준명 |
| korean_name | text | 한국어명 |
| english_name | text | 영문명 |
| abbreviation | text | 약어 |
| definition | text | 정의 |
| concept_type | enum | anatomy, disease 등 |
| external_ids | jsonb | MeSH 등 |

## 8.8 document_concepts

| 필드 | 타입 | 설명 |
|---|---|---|
| document_id | UUID | 문서 |
| concept_id | UUID | 개념 |
| importance | numeric | 중요도 |
| prerequisite | boolean | 선수지식 여부 |
| source_chunk_ids | UUID[] | 근거 조각 |

## 8.9 generated_contents

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| document_id | UUID | 문서 |
| user_id | UUID | 사용자 |
| content_type | enum | summary, lesson 등 |
| study_level | enum | 생성 수준 |
| content_json | jsonb | 구조화 결과 |
| model_provider | text | 공급자 |
| model_name | text | 모델 |
| prompt_version | text | 프롬프트 버전 |
| status | enum | draft, verified, failed |
| created_at | timestamptz | 생성일 |

## 8.10 claims

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| generated_content_id | UUID | 생성 콘텐츠 |
| claim_text | text | 주장 |
| claim_type | enum | fact, interpretation 등 |
| verification_status | enum | direct, inferred 등 |
| confidence | numeric | 검증 신뢰도 |
| warning | text | 주의 문구 |

## 8.11 claim_sources

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| claim_id | UUID | 주장 |
| source_type | enum | document, pubmed 등 |
| document_chunk_id | UUID | 내부 근거 |
| evidence_source_id | UUID | 외부 근거 |
| source_quote | text | 짧은 근거 구절 |
| support_score | numeric | 지지 정도 |
| page_number | integer | 페이지 |

## 8.12 qa_threads

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| user_id | UUID | 사용자 |
| document_id | UUID | 문서 |
| title | text | 대화 제목 |
| retrieval_scope | enum | document, library, external |
| created_at | timestamptz | 생성일 |

## 8.13 qa_messages

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| thread_id | UUID | 스레드 |
| role | enum | user, assistant |
| content | text | 메시지 |
| structured_content | jsonb | 답변 구조 |
| created_at | timestamptz | 생성일 |

## 8.14 evidence_sources

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| source | enum | pubmed, pmc 등 |
| pmid | text | PMID |
| pmcid | text | PMCID |
| doi | text | DOI |
| title | text | 제목 |
| abstract | text | 초록 |
| journal | text | 저널 |
| publication_date | date | 출판일 |
| study_type | enum | 연구 유형 |
| full_text_status | enum | none, open, uploaded |
| retraction_status | enum | unknown, clear, corrected, retracted |
| metadata_json | jsonb | 원본 메타데이터 |
| fetched_at | timestamptz | 수집일 |

## 8.15 evidence_searches

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| user_id | UUID | 사용자 |
| document_id | UUID | 관련 문서 |
| clinical_question | text | 질문 |
| question_type | enum | therapy, diagnosis 등 |
| pico_json | jsonb | PICO |
| query_text | text | 검색식 |
| executed_at | timestamptz | 검색일 |
| result_count | integer | 검색 결과 수 |

## 8.16 flashcards

| 필드 | 타입 | 설명 |
|---|---|---|
| id | UUID | 기본키 |
| user_id | UUID | 사용자 |
| document_id | UUID | 문서 |
| front | text | 앞면 |
| back | text | 뒷면 |
| card_type | enum | basic, cloze 등 |
| source_claim_id | UUID | 근거 |
| status | enum | active, archived |
| due_at | timestamptz | 복습 예정 |
| interval_days | integer | 간격 |
| ease_factor | numeric | 난이도 계수 |

## 8.17 quizzes, quiz_questions, quiz_attempts

퀴즈와 문제, 응시 기록을 분리한다.

필수 저장:

- 문제 유형
- 보기
- 정답
- 해설
- 출처
- 난이도
- 사용자 답변
- 정오답
- 오답 원인
- 소요 시간

## 8.18 user_concept_mastery

| 필드 | 타입 | 설명 |
|---|---|---|
| user_id | UUID | 사용자 |
| concept_id | UUID | 개념 |
| mastery_state | enum | 상태 |
| mastery_score | numeric | 숙련도 |
| last_studied_at | timestamptz | 마지막 학습 |
| next_review_at | timestamptz | 다음 복습 |
| evidence_count | integer | 평가 표본 수 |

---

# 9. API 명세

## 9.1 문서

### POST /api/documents

multipart 업로드.

응답:

```json
{
  "id": "uuid",
  "status": "uploaded",
  "duplicate": false
}
```

### GET /api/documents

필터:

- status
- document_type
- subject
- search
- cursor

### GET /api/documents/{document_id}

문서 메타데이터와 처리 상태 반환.

### DELETE /api/documents/{document_id}

옵션:

- delete_source
- delete_generated
- delete_study_records

### POST /api/documents/{document_id}/retry

실패한 처리 단계 재시도.

---

## 9.2 개인정보

### GET /api/documents/{document_id}/privacy-findings

탐지 결과 목록.

### PATCH /api/privacy-findings/{finding_id}

상태 변경:

- confirmed
- rejected

### POST /api/documents/{document_id}/redact

확정 항목으로 마스킹본 생성.

---

## 9.3 분석

### POST /api/documents/{document_id}/analyze

입력:

```json
{
  "study_level": 1,
  "generate_prerequisites": true,
  "generate_summary": true,
  "external_evidence": false
}
```

### GET /api/documents/{document_id}/analysis-status

단계와 진행률 반환.

### GET /api/documents/{document_id}/summaries

요약 목록.

### POST /api/documents/{document_id}/summaries/regenerate

수준 또는 유형 변경 후 재생성.

---

## 9.4 질의응답

### POST /api/qa/threads

### POST /api/qa/threads/{thread_id}/messages

입력:

```json
{
  "content": "이 연구의 결과를 쉽게 설명해 줘",
  "scope": "document",
  "study_level": 1,
  "include_external_evidence": false
}
```

응답은 스트리밍 가능하게 설계한다.

### GET /api/qa/threads/{thread_id}

메시지와 출처 포함.

---

## 9.5 근거 검색

### POST /api/evidence/search

입력:

```json
{
  "document_id": "uuid",
  "question": "HFrEF에서 SGLT2 억제제의 효과",
  "question_type": "therapy",
  "date_from": null,
  "study_types": ["systematic_review", "rct"]
}
```

### GET /api/evidence/searches/{search_id}

검색식, 검색일, 결과 목록.

### POST /api/evidence/compare

최대 5개 근거 비교.

---

## 9.6 플래시카드·퀴즈

### POST /api/documents/{document_id}/flashcards/generate

### GET /api/flashcards/due

### POST /api/flashcards/{card_id}/review

### POST /api/documents/{document_id}/quizzes/generate

### POST /api/quizzes/{quiz_id}/attempts

---

# 10. 백그라운드 작업

## 10.1 작업 종류

- validate_file
- extract_pdf
- run_ocr
- detect_privacy
- create_redacted_copy
- classify_document
- extract_sections
- extract_concepts
- build_embeddings
- generate_prerequisites
- generate_summary
- verify_claims
- generate_flashcards
- generate_quiz
- search_external_evidence
- refresh_retraction_status

## 10.2 작업 원칙

- 모든 작업은 idempotent하게 설계한다.
- 작업별 재시도 횟수를 제한한다.
- 외부 API 오류와 내부 오류를 구분한다.
- 처리 로그에 개인정보 원문을 남기지 않는다.
- 사용자가 문서를 삭제하면 대기 작업을 취소한다.

---

# 11. AI·RAG 파이프라인

## 11.1 문서 전처리

1. 페이지 텍스트 추출
2. 줄바꿈·하이픈 정규화
3. 머리말·꼬리말 제거
4. 섹션 탐지
5. 표·그림 캡션 분리
6. 참고문헌 분리
7. 문단 좌표 저장
8. 청크 생성
9. 임베딩 생성

## 11.2 청크 전략

- 기본 단위는 의미상 완결된 문단
- 결과 표는 행과 열의 의미를 보존
- 결과와 고찰을 섞지 않음
- 한 청크가 여러 페이지에 걸치면 페이지 범위를 저장
- 표의 수치와 단위를 함께 보존
- 참고문헌은 일반 본문 검색에서 낮은 우선순위

## 11.3 검색 전략

### 1차

현재 문서에서 키워드·벡터 혼합 검색.

### 2차

문서 구조 기반 재정렬.

- 질문이 연구 결과면 Results 우선
- 방법론 질문이면 Methods 우선
- 제한점이면 Discussion·Limitations 우선
- 용어 질문이면 정의와 최초 등장 문단 우선

### 3차

재순위화 모델 또는 규칙 기반 점수.

### 4차

답변에 실제로 사용된 조각만 출처로 저장.

---

# 12. 생성 결과 JSON 구조

## 12.1 요약

```json
{
  "document_overview": {
    "topic": "",
    "document_type": "",
    "one_sentence_summary": ""
  },
  "key_points": [
    {
      "text": "",
      "claim_id": "",
      "importance": "high"
    }
  ],
  "prerequisites": [
    {
      "concept": "",
      "reason": "",
      "difficulty": "basic"
    }
  ],
  "limitations": [],
  "uncertainties": []
}
```

## 12.2 질의응답

```json
{
  "direct_answer": "",
  "simple_explanation": "",
  "clinical_meaning": "",
  "cautions": [],
  "claims": [
    {
      "text": "",
      "source_refs": [],
      "verification_status": "direct"
    }
  ],
  "not_found": [],
  "confidence": "medium"
}
```

## 12.3 논문 분석

```json
{
  "research_question": "",
  "study_design": "",
  "population": "",
  "intervention": "",
  "comparator": "",
  "outcomes": [],
  "main_results": [],
  "limitations": [],
  "applicability": "",
  "statistical_vs_clinical_significance": ""
}
```

---

# 13. 프롬프트 원칙

## 13.1 공통 시스템 규칙

- 서비스 목적은 교육이다.
- 실제 진단·처방을 제공하지 않는다.
- 제공된 출처에 없는 사실을 문서 내용처럼 말하지 않는다.
- 수치에는 단위, 대상, 시점을 포함한다.
- 상관관계를 인과관계로 바꾸지 않는다.
- 연구 결과와 저자의 해석을 구분한다.
- 통계적 유의성과 임상적 중요성을 구분한다.
- 불확실하면 불확실하다고 답한다.
- 모든 핵심 주장에 source_ref를 연결한다.
- 출력은 지정된 JSON 스키마를 따른다.

## 13.2 학생 수준 변환 규칙

### Level 0

- 한 문장에 한 개념
- 전문용어 직후 괄호 설명
- 비유 사용 가능
- 비유가 실제 기전과 다른 부분 명시

### Level 1

- 정상 생리부터 설명
- 질환의 변화 과정을 순서대로 설명
- 핵심 용어 유지

### Level 2

- 감별진단과 검사 선택 이유 포함
- 임상적 예외 포함

### Level 3

- 연구 설계와 편향
- 효과크기
- 적용 가능성
- 근거 간 불일치

---

# 14. 주장 검증 파이프라인

## 14.1 단계

1. 생성 답변에서 원자적 주장 추출
2. 각 주장에 연결된 출처 확인
3. 출처가 주장을 직접 지지하는지 평가
4. 수치·단위·방향 확인
5. 대상 집단 확인
6. 연구 설계 확인
7. 과장 여부 확인
8. 상태 부여
9. 실패 문장 제거 또는 경고

## 14.2 검증 상태 정의

### direct

원문에 동일한 의미가 명시됨.

### inferred

원문 여러 부분을 조합한 제한적 추론.

### external

외부 근거에 의해 확인됨.

### synthesized

여러 근거를 종합함.

### insufficient

근거가 부족함.

### contradicted

출처와 충돌함.

### not_found

자료에 없음.

## 14.3 차단 규칙

다음 문장은 사용자에게 그대로 노출하지 않는다.

- 출처와 반대되는 주장
- 대상 환자군이 완전히 다른데 일반화한 주장
- 단위가 불명확한 수치
- 근거 없이 약물 용량을 제안하는 문장
- 실제 진단을 확정하는 문장
- 응급 상황에서 행동을 지연시킬 수 있는 문장

---

# 15. 의료 안전 요구사항

## 15.1 고정 고지

- 학습용 서비스임을 가입·업로드·질의응답 화면에 표시
- 실제 환자 판단에 사용하지 말 것
- 응급 상황에는 지역 응급의료체계를 이용할 것
- AI 설명은 출처를 확인할 것

## 15.2 의료 질문 분류

질문 입력 시 다음 유형을 탐지한다.

- 학습 질문
- 실제 환자 관련 질문
- 개인 증상 질문
- 약물 용량 질문
- 응급 가능 질문

학습 질문만 정상 처리한다.

실제 환자 질문은 교육적 일반 정보로 제한하고 개인 진단을 거부한다.

## 15.3 감사 로그

기록 대상:

- 문서 업로드
- 마스킹 승인
- 외부 AI 전송
- 외부 근거 검색
- 생성 콘텐츠 버전
- 사용자 수정
- 문서 삭제

개인정보 원문은 감사 로그에 저장하지 않는다.

---

# 16. 보안 요구사항

- 인증된 사용자만 개인 문서 접근
- 모든 문서 조회에 소유권 검사
- 객체 저장소는 비공개
- 다운로드는 짧은 만료시간의 서명 URL 사용
- 데이터베이스 연결 암호화
- 민감 데이터 암호화
- 원본 개인정보와 마스킹본 분리
- API rate limit
- 파일 업로드 크기 제한
- CSRF·XSS·SQL Injection 방어
- 모델 공급자 API 키 서버 보관
- 프롬프트와 로그에 비밀키 출력 금지
- 사용자 삭제 요청 처리
- 백업 데이터 보존 기간 정의

---

# 17. 비기능 요구사항

## 17.1 성능

- 일반 PDF 업로드 응답은 즉시 작업 ID 반환
- PDF 뷰어는 분석 완료 전에도 원문 표시
- 질의응답은 스트리밍 지원
- 긴 작업은 비동기 큐 처리
- 페이지 이동과 출처 하이라이트는 체감 지연 최소화

## 17.2 신뢰성

- 작업 재시도
- 작업 중복 방지
- 부분 실패 허용
- 외부 검색 실패 시 내부 문서 학습 유지
- AI 공급자 장애 시 대체 공급자 또는 재시도

## 17.3 관찰 가능성

- 구조화 로그
- 작업별 처리 시간
- 오류율
- OCR 사용률
- 출처 검증 실패율
- 외부 모델 비용
- 검색 결과 클릭률
- 사용자 오류 신고

---

# 18. 테스트 계획

## 18.1 단위 테스트

- 파일 검증
- 텍스트 정규화
- 개인정보 정규식
- 문서 권한 검사
- 인용 위치 변환
- 연구 유형 분류 후처리
- JSON 스키마 검증

## 18.2 통합 테스트

- PDF 업로드부터 분석 완료
- 마스킹본 생성
- 임베딩 저장
- 문서 기반 질의응답
- 출처 클릭 이동
- 근거 검색
- 플래시카드 생성

## 18.3 AI 평가 세트

최소 다음 자료를 포함한다.

- 텍스트 기반 강의 PDF
- 2단 편집 논문
- 표가 많은 임상시험
- 스캔 품질이 낮은 문서
- 한국어·영어 혼합 자료
- 증례보고
- 참고문헌이 긴 종설
- 수치가 서로 유사한 문서

## 18.4 평가 지표

- 문서 유형 정확도
- 섹션 분류 정확도
- 페이지 인용 정확도
- 주장-출처 지지율
- 수치 일치율
- 개인정보 탐지 재현율
- 잘못된 의료 주장 비율
- 사용자가 이해했다고 평가한 비율

## 18.5 E2E 시나리오

### 시나리오 A

1. 사용자가 심부전 논문 업로드
2. 개인정보 없음 확인
3. 학생용 요약 생성
4. “박출률이 뭐야?” 질문
5. 쉬운 설명 제공
6. PDF 페이지 출처 이동
7. 플래시카드 생성

### 시나리오 B

1. 익명화되지 않은 케이스 PDF 업로드
2. 개인정보 탐지
3. 사용자 마스킹 승인
4. 외부 AI 전송
5. 감사 로그 확인

### 시나리오 C

1. 문서에 없는 약물 용량 질문
2. “자료에 없음” 표시
3. 실제 처방 제안 금지
4. 일반 학습 정보만 제공

---

# 19. MVP 완료 기준

다음 조건을 모두 만족해야 v0.1을 완료로 본다.

1. PDF 업로드와 삭제가 안정적으로 동작한다.
2. 페이지별 텍스트와 원문 좌표가 저장된다.
3. 학생 수준별 요약이 생성된다.
4. 핵심 문장의 90% 이상에 출처 상태가 존재한다.
5. 출처 클릭 시 올바른 페이지로 이동한다.
6. 문서에 없는 질문에 대해 존재하는 것처럼 답하지 않는다.
7. 개인정보 검토 후에만 외부 모델을 호출할 수 있다.
8. 플래시카드와 퀴즈에 출처가 연결된다.
9. 주요 작업에 자동화 테스트가 존재한다.
10. 실제 진단·처방을 차단하는 안전 테스트를 통과한다.

---

# 20. 개발 단계

## Sprint 0: 기반 구축

- 모노레포 생성
- 개발 환경
- PostgreSQL·Redis·객체 저장소
- 인증
- 공통 타입
- CI
- 로깅
- 환경 변수 검증

## Sprint 1: 문서 업로드

- 파일 업로드
- 안전 검사
- 문서 목록
- 상태 표시
- PDF 뷰어
- 작업 큐

## Sprint 2: 파싱·개인정보

- 페이지 추출
- OCR 분기
- 섹션 구조
- 개인정보 탐지
- 검토 UI
- 마스킹본

## Sprint 3: 검색·요약

- 청크 생성
- 임베딩
- 하이브리드 검색
- 다단계 요약
- 선수지식
- 구조화 출력

## Sprint 4: 출처 기반 Q&A

- 대화 스레드
- 스트리밍 답변
- 주장 추출
- 출처 검증
- PDF 위치 이동
- 답변 평가

## Sprint 5: 복습

- 플래시카드
- 퀴즈
- 오답 분석
- 학습 진도

## Sprint 6: 외부 근거 베타

- PubMed 검색
- PICO
- 연구 유형
- 근거 카드
- 검색식·검색일 저장
- 문서와 비교

## Sprint 7: 안정화

- AI 평가 세트
- 보안 검토
- 성능 개선
- 오류 복구
- 사용성 개선
- 배포 문서

---

# 21. Claude Code용 통합 구현 지시문

아래 지시문은 새 저장소에서 작업하는 것을 전제로 한다.

## Master Prompt

당신은 MedBridge Study의 수석 풀스택 엔지니어다.

이 프로젝트는 사용자가 업로드한 의학 PDF를 학생 수준으로 설명하고, 모든 핵심 주장에 원문 페이지 또는 외부 근거를 연결하는 개인 의학 학습 서비스다. 실제 환자의 진단, 처방, 응급 판단을 수행해서는 안 된다.

### 기술 기준

- Monorepo
- Frontend: Next.js App Router, TypeScript
- Backend: FastAPI, Python
- Database: PostgreSQL
- Vector search: pgvector
- Queue: Redis 기반 작업 큐
- Object storage: S3-compatible storage
- PDF rendering: PDF.js
- Migrations: Alembic
- Validation: Pydantic and shared TypeScript schemas where practical
- Testing: backend unit/integration tests, frontend component tests, E2E tests
- Local development: Docker Compose

### 필수 설계 원칙

1. 원본 PDF와 마스킹 PDF를 분리한다.
2. 외부 AI에는 사용자가 허용한 마스킹본만 전송한다.
3. 모든 문서 청크는 page_number와 bbox를 가져야 한다.
4. 생성 답변의 핵심 주장은 Claim 엔터티로 저장한다.
5. Claim에는 하나 이상의 출처 또는 근거 불충분 상태가 있어야 한다.
6. 결과, 방법, 고찰 섹션을 가능한 한 구분한다.
7. 문서에 없는 정보를 문서에 있는 것처럼 답하지 않는다.
8. 수치에는 단위와 대상 집단을 보존한다.
9. 실제 진단과 처방 요청은 차단한다.
10. 모델 공급자에 종속되지 않는 provider interface를 만든다.
11. 모든 비동기 작업은 idempotent하게 만든다.
12. 사용자 소유권 검사를 모든 문서 API에 적용한다.
13. 사용자에게 보이는 오류는 실패 단계와 복구 방법을 포함한다.
14. 개인정보 원문을 로그에 남기지 않는다.
15. 구현 후 테스트와 문서를 함께 갱신한다.

### 1차 목표

다음 사용자 흐름을 완성한다.

1. 사용자 로그인
2. PDF 업로드
3. 파일 검증
4. 페이지별 텍스트 추출
5. 개인정보 후보 탐지
6. 사용자 검토
7. 마스킹본 생성
8. 문서 구조 및 청크 생성
9. 학생용 요약 생성
10. 주장별 페이지 출처 저장
11. 문서 기반 질문
12. 답변 출처 클릭 시 PDF 페이지 이동
13. 플래시카드 생성
14. 문서 삭제

### 구현 순서

작업을 한 번에 모두 작성하지 말고 다음 순서로 진행한다.

#### Step 1. 저장소 조사 및 계획

- 기존 파일과 설정을 확인한다.
- 이미 존재하는 패턴을 우선 사용한다.
- 구현 계획을 `docs/implementation-plan.md`에 작성한다.
- 변경할 파일과 데이터 흐름을 명시한다.
- 불필요한 대규모 리팩터링을 피한다.

#### Step 2. 기반 인프라

- apps/web, apps/api, apps/worker 구조
- Docker Compose
- PostgreSQL, Redis, object storage
- 환경 변수 스키마와 `.env.example`
- health check
- 기본 CI

#### Step 3. 데이터 모델

다음 테이블을 우선 구현한다.

- users
- documents
- document_pages
- document_sections
- document_chunks
- privacy_findings
- generated_contents
- claims
- claim_sources
- qa_threads
- qa_messages
- flashcards

모든 마이그레이션에 downgrade를 제공한다.

#### Step 4. 업로드와 처리 상태

- multipart upload
- 파일 유형·크기·해시 검사
- UUID 저장 경로
- 처리 상태 머신
- 백그라운드 작업 생성
- 중복 파일 탐지
- 문서 목록과 상세 화면

#### Step 5. PDF 파싱

- 페이지별 텍스트 추출
- 페이지 번호 보존
- 문단 bbox 저장
- 텍스트 추출 비율로 OCR 필요 여부 판단
- 파싱 신뢰도 저장
- 손상 페이지가 있어도 가능한 페이지는 유지

#### Step 6. 개인정보

- 규칙 기반 탐지부터 구현
- 이름과 주소는 모델 기반 탐지를 확장 가능하게 인터페이스화
- 개인정보 검토 UI
- confirmed 항목만 마스킹
- 외부 AI 호출 전 redacted copy 존재를 검사

#### Step 7. RAG

- 문단 기반 청크
- pgvector 임베딩
- 키워드와 벡터 혼합 검색
- 질문 유형에 따라 섹션 가중치 적용
- 답변에 사용한 chunk id 저장

#### Step 8. 요약과 Claim

- 구조화 JSON 생성
- key point마다 Claim 생성
- ClaimSource에 chunk 연결
- 별도 검증 단계 실행
- contradicted 또는 unsupported 문장을 사용자 출력에서 제거

#### Step 9. 문서 Q&A

- 문서 단위 thread
- streaming response
- 답변 JSON 스키마 검증
- 직접 답변, 쉬운 설명, 주의점, 출처 섹션
- 자료에 없는 경우 not_found 상태

#### Step 10. PDF 출처 UX

- citation badge
- 클릭 시 페이지 이동
- bbox가 있으면 하이라이트
- 출처 패널에서 짧은 원문 확인
- 추출 신뢰도가 낮으면 경고 표시

#### Step 11. 플래시카드

- 현재 문서에서 카드 생성
- 카드마다 source_claim_id 저장
- 수정, 삭제, 보관
- 간단한 복습 기록

#### Step 12. 테스트

최소 다음 테스트를 구현한다.

- 타 사용자 문서 접근 차단
- 비PDF 업로드 거부
- 중복 파일 처리
- 개인정보 확정 전 외부 AI 호출 차단
- 문서 없는 주장 차단
- 페이지 인용 저장
- 문서 삭제 시 파생 데이터 처리
- 실제 진단·처방 요청 차단

### 코딩 규칙

- 타입을 명확히 정의한다.
- `any` 사용을 피한다.
- API 오류 응답 형식을 통일한다.
- 서비스 계층과 라우터를 분리한다.
- 모델 호출 코드를 비즈니스 로직에 직접 섞지 않는다.
- 프롬프트를 코드 문자열로 흩어놓지 않고 버전 관리한다.
- UI에는 loading, empty, error 상태가 모두 있어야 한다.
- 비동기 작업에는 correlation id를 사용한다.
- 모든 외부 호출에는 timeout을 설정한다.
- 재시도는 지수 백오프를 사용하되 무한 재시도하지 않는다.
- 보안상 중요한 변경은 테스트 없이 완료 처리하지 않는다.

### 작업 보고 형식

각 단계 완료 후 다음을 보고한다.

1. 구현한 기능
2. 변경 파일
3. 데이터 흐름
4. 테스트 결과
5. 남은 위험
6. 다음 작업

### 금지 사항

- 근거 없는 의료 문장을 하드코딩하지 말 것
- 실제 환자에 대한 진단 결과를 생성하지 말 것
- 개인정보를 콘솔에 출력하지 말 것
- 외부 논문 전문을 라이선스 확인 없이 저장하지 말 것
- 출처가 없는 답변을 높은 확신으로 표시하지 말 것
- 테스트를 삭제하거나 무력화하여 CI를 통과시키지 말 것

먼저 저장소를 조사하고 `docs/implementation-plan.md`를 작성한 뒤, Sprint 0과 Sprint 1 범위만 구현하라. 구현 전 현재 구조와 충돌 가능성을 설명하고, 기존 코드가 있으면 그 패턴을 유지하라.
