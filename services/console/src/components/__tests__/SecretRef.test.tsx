import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SecretRef } from "../SecretRef";

describe("SecretRef Component Security & Masking", () => {
  const mockClipboard = {
    writeText: vi.fn().mockResolvedValue(undefined),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    Object.assign(navigator, {
      clipboard: mockClipboard,
    });
  });

  it("renders empty state when no credentialRef provided", () => {
    render(<SecretRef credentialRef={null} />);
    expect(screen.getByText("未配置凭据引用")).toBeInTheDocument();
  });

  it("masks credential reference by default", () => {
    const rawRef = "vault:secret/data/agents/banking#api_key";
    render(<SecretRef credentialRef={rawRef} />);

    // By default, raw ref should be masked (vaul***)
    expect(screen.getByText("vaul***")).toBeInTheDocument();
    expect(screen.queryByText(rawRef)).not.toBeInTheDocument();
  });

  it("reveals and toggles mask when toggle button is clicked", () => {
    const rawRef = "vault:secret/data/agents/banking#api_key";
    render(<SecretRef credentialRef={rawRef} />);

    const toggleBtn = screen.getByTitle("显示引用名");
    fireEvent.click(toggleBtn);

    // Now raw ref is visible
    expect(screen.getByText(rawRef)).toBeInTheDocument();

    // Click again to hide
    const hideBtn = screen.getByTitle("隐藏引用名");
    fireEvent.click(hideBtn);
    expect(screen.getByText("vaul***")).toBeInTheDocument();
  });

  it("copies credential reference to clipboard on click", async () => {
    const rawRef = "env://AGENT_SECRET_KEY";
    render(<SecretRef credentialRef={rawRef} />);

    const copyBtn = screen.getByTitle("复制引用");
    fireEvent.click(copyBtn);

    expect(mockClipboard.writeText).toHaveBeenCalledWith(rawRef);
  });
});
