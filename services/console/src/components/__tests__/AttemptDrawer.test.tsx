import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { AttemptDrawer } from "../../features/launches/AttemptDrawer";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: {
    GET: vi.fn(),
  },
}));

const renderDrawer = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AttemptDrawer
        isOpen
        onClose={vi.fn()}
        itemExecutionId="item-1"
        caseId="case-1"
      />
    </QueryClientProvider>,
  );
};

describe("AttemptDrawer empty state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.GET as any).mockResolvedValue({ data: [] });
  });

  it("uses the shared EmptyState primitive when a case has no attempts", async () => {
    renderDrawer();

    // Every other route in the console renders this through `EmptyState`;
    // a hand-rolled `<div>` here is the same visual pattern with none of the
    // primitive's structure — dashed border, icon, and the title/description
    // hierarchy that `axe` and screen readers rely on.
    const empty = await screen.findByTestId("empty-state");
    expect(empty).toBeInTheDocument();
    expect(empty).toHaveTextContent("暂无记录的调用 Attempt");
  });
});

it("shows only credential identity and revision for each actual Header", async () => {
  vi.mocked(api.GET).mockResolvedValue({ data: [{ id: "attempt-1", attempt_no: 1, status: "SUCCEEDED", started_at: "2026-11-06T00:00:00Z", credential_usage: [
    { credential_id: "credential-a", version: 2, provider: "managed", header_name: "authorization" },
    { credential_id: "credential-b", version: 3, provider: "managed", header_name: "x-extra" },
  ] }] } as any);
  renderDrawer();
  expect(await screen.findByText("authorization · credential-a · 修订 2")).toBeVisible();
  expect(screen.getByText("x-extra · credential-b · 修订 3")).toBeVisible();
});
