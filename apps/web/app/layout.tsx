import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "MedBridge Study",
  description: "PDF 기반 개인 의학 학습 도우미",
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
          <header className="border-b border-slate-200 bg-white">
            <nav
              aria-label="주 메뉴"
              className="mx-auto flex w-full max-w-5xl items-center gap-4 px-4 py-3"
            >
              <Link href="/documents" className="text-lg font-bold text-blue-700">
                MedBridge Study
              </Link>
              <div className="ml-auto flex items-center gap-3 text-sm">
                <Link href="/documents" className="rounded px-2 py-1 hover:bg-slate-100">
                  내 학습자료
                </Link>
                <Link href="/settings" className="rounded px-2 py-1 hover:bg-slate-100">
                  앱 설정
                </Link>
              </div>
            </nav>
          </header>
          <main className="flex-1">{children}</main>
          <footer className="border-t border-slate-200 bg-white px-6 py-3 text-xs text-slate-500">
            MedBridge Study는 의학 학습 보조 도구입니다. 실제 환자의 진단·처방·응급 판단에
            사용하지 마세요. 응급 상황에서는 119에 연락하세요.
          </footer>
        </Providers>
      </body>
    </html>
  );
}
