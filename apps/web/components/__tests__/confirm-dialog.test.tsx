import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ConfirmDialog, PromptDialog } from "@/components/confirm-dialog";

describe("ConfirmDialog", () => {
  it("열리면 초점이 대화상자 안으로 들어간다", () => {
    // window.confirm은 OS가 초점을 가져가지만 앱 안의 다이얼로그는 직접 옮겨야 한다.
    // 옮기지 않으면 스크린리더는 "대화상자가 열렸다"고 말하면서 정작 읽을 위치는
    // 뒷배경에 남아, 사용자는 무엇을 확인하라는 것인지 알 수 없다.
    render(
      <ConfirmDialog
        open
        title="이 대화를 삭제할까요?"
        confirmLabel="삭제"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const dialog = screen.getByRole("alertdialog", { name: "이 대화를 삭제할까요?" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    // 파괴적 동작이므로 기본 초점은 취소에 둔다 — Enter를 습관적으로 눌러도 지워지지 않게.
    expect(within(dialog).getByRole("button", { name: "취소" })).toHaveFocus();
  });

  it("Esc를 누르면 취소된다", async () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        open
        title="삭제할까요?"
        confirmLabel="삭제"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );

    await userEvent.keyboard("{Escape}");

    expect(onCancel).toHaveBeenCalledOnce();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Tab이 대화상자 밖으로 나가지 않는다", async () => {
    // 초점이 뒷배경으로 새면 사용자는 잠긴 줄 알았던 화면을 조작하게 되고,
    // 스크린리더는 대화상자 밖 내용을 계속 읽어 무엇이 열려 있는지 잃는다.
    render(
      <>
        <button type="button">뒷배경 버튼</button>
        <ConfirmDialog
          open
          title="삭제할까요?"
          confirmLabel="삭제"
          onConfirm={vi.fn()}
          onCancel={vi.fn()}
        />
      </>,
    );

    const dialog = screen.getByRole("alertdialog");
    const cancel = within(dialog).getByRole("button", { name: "취소" });
    const confirm = within(dialog).getByRole("button", { name: "삭제" });

    expect(cancel).toHaveFocus();
    await userEvent.tab();
    expect(confirm).toHaveFocus();
    await userEvent.tab(); // 마지막에서 한 번 더 — 첫 요소로 돌아와야 한다
    expect(cancel).toHaveFocus();
  });

  it("확인을 누르면 onConfirm이 불린다", async () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        open
        title="삭제할까요?"
        description="되돌릴 수 없습니다."
        confirmLabel="삭제"
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByText("되돌릴 수 없습니다.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "삭제" }));

    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it("닫혀 있으면 아무것도 그리지 않는다", () => {
    render(
      <ConfirmDialog
        open={false}
        title="삭제할까요?"
        confirmLabel="삭제"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});

describe("PromptDialog", () => {
  it("현재 값이 채워진 채 열리고 초점이 입력란에 간다", () => {
    render(
      <PromptDialog
        open
        title="새 이름을 입력해 주세요."
        label="자료 이름"
        defaultValue="심장학 3판"
        confirmLabel="바꾸기"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const input = screen.getByLabelText("자료 이름");
    expect(input).toHaveValue("심장학 3판");
    expect(input).toHaveFocus();
  });

  it("앞뒤 공백을 없앤 값을 넘긴다", async () => {
    const onConfirm = vi.fn();
    render(
      <PromptDialog
        open
        title="새 이름"
        label="자료 이름"
        defaultValue="옛 이름"
        confirmLabel="바꾸기"
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );

    const input = screen.getByLabelText("자료 이름");
    await userEvent.clear(input);
    await userEvent.type(input, "  새 이름  ");
    await userEvent.click(screen.getByRole("button", { name: "바꾸기" }));

    expect(onConfirm).toHaveBeenCalledWith("새 이름");
  });

  it("빈 이름으로는 확인할 수 없다", async () => {
    // window.prompt는 빈 문자열을 그대로 돌려주고 호출부가 매번 걸러야 했다.
    // 여기서 막으면 "왜 아무 일도 안 일어나지"가 아니라 버튼이 눌리지 않는 것이 보인다.
    const onConfirm = vi.fn();
    render(
      <PromptDialog
        open
        title="새 이름"
        label="자료 이름"
        defaultValue="옛 이름"
        confirmLabel="바꾸기"
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.clear(screen.getByLabelText("자료 이름"));
    const confirm = screen.getByRole("button", { name: "바꾸기" });

    expect(confirm).toBeDisabled();
    await userEvent.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Enter로도 확인된다", async () => {
    const onConfirm = vi.fn();
    render(
      <PromptDialog
        open
        title="새 이름"
        label="자료 이름"
        defaultValue="옛 이름"
        confirmLabel="바꾸기"
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );

    await userEvent.type(screen.getByLabelText("자료 이름"), "{Enter}");

    expect(onConfirm).toHaveBeenCalledWith("옛 이름");
  });
});
