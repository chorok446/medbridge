/** 브랜드 마크 — DESIGN.md의 플랫 규칙에서 그라디언트가 허용되는 유일한 자리다.
 *
 * 원본(`docs/brand/medbridge-logo-concept-1.svg`)의 drop-shadow는 뺐다. 헤더 크기에서는
 * 형태를 흐릿하게 만들 뿐이고, 그림자를 UI로 들여오는 통로가 되어서도 안 된다.
 */
export function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 150 150"
      role="img"
      aria-label="MedBridge Study"
    >
      <defs>
        <linearGradient id="mb-mark" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#2563EB" />
          <stop offset="100%" stopColor="#0D9488" />
        </linearGradient>
      </defs>
      <rect width="150" height="150" rx="38" fill="url(#mb-mark)" />
      <rect x="31" y="32" width="20" height="84" rx="10" fill="#FFFFFF" />
      <rect x="99" y="32" width="20" height="84" rx="10" fill="#FFFFFF" />
      <path
        d="M41 74 C54 42,96 42,109 74"
        fill="none"
        stroke="#FFFFFF"
        strokeWidth="14"
        strokeLinecap="round"
      />
      <path
        d="M42 99 H108"
        fill="none"
        stroke="#CCFBF1"
        strokeWidth="11"
        strokeLinecap="round"
      />
      <rect x="70" y="70" width="10" height="34" rx="5" fill="#0F766E" />
      <rect x="58" y="82" width="34" height="10" rx="5" fill="#0F766E" />
    </svg>
  );
}
