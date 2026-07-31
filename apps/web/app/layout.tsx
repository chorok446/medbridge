import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "MedBridge Study",
  description: "PDF·임상 케이스 기반 개인 의학 학습 서비스",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ko" className="h-full antialiased">
      <body className="min-h-full flex flex-col bg-slate-50 text-slate-900">
        <Providers>
          <main className="flex-1">{children}</main>
          <footer className="border-t border-slate-200 bg-white px-6 py-3 text-xs text-slate-500">
            MedBridge Study는 의학 학습 보조 도구입니다. 실제 환자의 진단·처방·응급 판단에
            사용하지 마세요. 응급 상황에서는 지역 응급의료체계(119)를 이용하세요.
          </footer>
        </Providers>
      </body>
    </html>
  );
}
