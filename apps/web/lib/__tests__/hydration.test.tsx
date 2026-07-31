/**
 * 정적 export(next build)는 window가 없는 Node에서 한 번 렌더된다 — 그때
 * isTauri()는 항상 false다. 실제 Tauri 웹뷰에서는 true다. 컴포넌트가 이
 * 값을 렌더 중 직접 분기에 쓰면, 빌드가 만든 정적 HTML과 실제 런타임의
 * 첫 클라이언트 렌더가 달라져 React #418(하이드레이션 불일치)이 난다.
 *
 * 이 테스트는 실제 React 하이드레이션 경로(hydrateRoot)로 재현한다:
 * 1) __TAURI_INTERNALS__ 없이 정적 HTML을 만들고(빌드 시뮬레이션)
 * 2) __TAURI_INTERNALS__를 채운 뒤 그 HTML을 hydrateRoot로 되살리며
 *    console.error에 하이드레이션 불일치 경고가 뜨는지 확인한다.
 */
import { act } from "react";
import { hydrateRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StartupGate } from "@/components/startup-gate";
import { UpdateManager } from "@/components/update-manager";

function expectNoHydrationMismatch(jsx: React.ReactElement) {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  const staticHtml = renderToStaticMarkup(jsx);

  Object.defineProperty(window, "__TAURI_INTERNALS__", {
    value: {},
    configurable: true,
  });

  const container = document.createElement("div");
  container.innerHTML = staticHtml;
  document.body.appendChild(container);

  const messages: unknown[] = [];
  const spy = vi.spyOn(console, "error").mockImplementation((msg: unknown) => {
    messages.push(msg);
  });

  act(() => {
    hydrateRoot(container, jsx);
  });

  spy.mockRestore();
  document.body.removeChild(container);

  const mismatch = messages.some(
    (m) => typeof m === "string" && /hydrat/i.test(m),
  );
  expect(mismatch).toBe(false);
}

describe("Tauri 런타임에서 하이드레이션 불일치가 나지 않는다 (React #418 회귀)", () => {
  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  });

  it("StartupGate", () => {
    expectNoHydrationMismatch(
      <StartupGate>
        <p>메인 화면</p>
      </StartupGate>,
    );
  });

  it("UpdateManager (레이아웃의 autoCheck 모드)", () => {
    expectNoHydrationMismatch(<UpdateManager autoCheck />);
  });
});
