import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StartupGate } from "@/components/startup-gate";
import { UpdateManager } from "@/components/update-manager";

describe("StartupGate (브라우저 모드)", () => {
  it("Tauri가 아니면 즉시 자식을 렌더링한다", () => {
    render(
      <StartupGate>
        <p>메인 화면</p>
      </StartupGate>,
    );
    expect(screen.getByText("메인 화면")).toBeInTheDocument();
  });
});

describe("UpdateManager (브라우저 모드)", () => {
  it("수동 모드는 데스크톱 전용 안내를 보여준다", () => {
    render(<UpdateManager />);
    expect(screen.getByText(/데스크톱 앱에서 확인할 수 있어요/)).toBeInTheDocument();
  });

  it("자동 확인 모드는 아무것도 렌더링하지 않는다", () => {
    const { container } = render(<UpdateManager autoCheck />);
    expect(container).toBeEmptyDOMElement();
  });
});
