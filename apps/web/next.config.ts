import type { NextConfig } from "next";

// 브라우저 → Next 서버 → API 프록시.
// 같은 오리진으로 세션 쿠키를 유지하고, API 내부 주소·저장소 자격증명을 클라이언트에 노출하지 않는다.
const API_INTERNAL_URL = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_INTERNAL_URL}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
