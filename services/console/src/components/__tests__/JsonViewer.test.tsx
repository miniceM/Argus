import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { JsonViewer } from "../JsonViewer";

describe("JsonViewer Component", () => {
  const mockClipboard = {
    writeText: vi.fn().mockResolvedValue(undefined),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    Object.assign(navigator, {
      clipboard: mockClipboard,
    });
  });

  it("renders JSON data formatted as string", () => {
    const data = { agent: "test-agent", version: "1.0.0" };
    render(<JsonViewer data={data} title="Test Manifest" />);

    expect(screen.getByText("Test Manifest")).toBeInTheDocument();
    expect(screen.getByText(/test-agent/)).toBeInTheDocument();
  });

  it("copies JSON formatted text to clipboard on button click", async () => {
    const data = { key: "value" };
    render(<JsonViewer data={data} />);

    const copyBtn = screen.getByText("复制");
    fireEvent.click(copyBtn);

    expect(mockClipboard.writeText).toHaveBeenCalledWith(JSON.stringify(data, null, 2));
    expect(await screen.findByText("已复制")).toBeInTheDocument();
  });
});
