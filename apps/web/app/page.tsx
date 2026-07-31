"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function Home() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/documents");
  }, [router]);
  return (
    <div className="flex min-h-[50vh] items-center justify-center text-sm text-slate-500">
      <Link href="/documents">학습자료로 이동 중…</Link>
    </div>
  );
}
