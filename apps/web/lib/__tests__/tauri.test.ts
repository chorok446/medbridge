import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OLLAMA_INSTALL_URL, openExternalUrl } from "@/lib/tauri";

const openerMock = vi.hoisted(() => ({ openUrl: vi.fn() }));
vi.mock("@tauri-apps/plugin-opener", () => openerMock);

function markAsTauri() {
  Object.defineProperty(window, "__TAURI_INTERNALS__", {
    value: {},
    configurable: true,
  });
}

describe("openExternalUrl", () => {
  beforeEach(() => {
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
    openerMock.openUrl.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  });

  it("Tauri에서는 allowlist의 공식 Ollama URL만 opener로 연다", async () => {
    markAsTauri();
    openerMock.openUrl.mockResolvedValue(undefined);

    await expect(openExternalUrl(OLLAMA_INSTALL_URL)).resolves.toBe(true);

    expect(openerMock.openUrl).toHaveBeenCalledWith("https://ollama.com/download/windows");
  });

  it("브라우저에서는 팝업의 opener를 끊은 뒤 공식 URL로 이동한다", async () => {
    const popup = {
      opener: window,
      location: { href: "about:blank" },
      close: vi.fn(),
    } as unknown as Window;
    const open = vi.spyOn(window, "open").mockReturnValue(popup);

    await expect(openExternalUrl(OLLAMA_INSTALL_URL)).resolves.toBe(true);

    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(popup.opener).toBeNull();
    expect(popup.location.href).toBe(OLLAMA_INSTALL_URL);
  });

  it("브라우저가 팝업을 차단하면 false를 반환한다", async () => {
    vi.spyOn(window, "open").mockReturnValue(null);

    await expect(openExternalUrl(OLLAMA_INSTALL_URL)).resolves.toBe(false);
  });

  it("https라도 allowlist 밖의 URL은 Tauri와 브라우저 모두 거부한다", async () => {
    const open = vi.spyOn(window, "open");

    await expect(openExternalUrl("https://example.com/download")).rejects.toThrow(
      "허용되지 않은 외부 URL",
    );
    expect(open).not.toHaveBeenCalled();
    expect(openerMock.openUrl).not.toHaveBeenCalled();
  });
});
