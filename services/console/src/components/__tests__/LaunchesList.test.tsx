import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { LaunchesList, LAUNCH_STATUSES } from "../../features/launches/LaunchesList";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: {
    GET: vi.fn(),
  },
}));

describe("LaunchesList Status Filter Contract and Behavior (Issue #24)", () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    vi.clearAllMocks();
    queryClient = new QueryClient({
      defaultOptions: {
        queries: {
          retry: false,
        },
      },
    });
    vi.mocked(api.GET).mockResolvedValue({
      data: [],
      error: undefined,
      response: new Response(),
    } as any);
  });

  const renderComponent = () => {
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <LaunchesList />
        </MemoryRouter>
      </QueryClientProvider>
    );
  };

  it("status filter options must exactly match the 8 canonical LaunchStatus enum values", () => {
    renderComponent();

    const selectElements = screen.getAllByRole("combobox");
    // The first select is execution status, the second is quality conclusion
    const statusSelect = selectElements[0] as HTMLSelectElement;
    expect(statusSelect).toBeInTheDocument();

    const options = Array.from(statusSelect.options);
    const nonDefaultOptionValues = options.slice(1).map((opt) => opt.value);

    // Expected exactly 8 canonical states
    const expectedStatuses = [
      "PENDING",
      "QUEUED",
      "RUNNING",
      "COMPLETED",
      "PARTIAL_FAILED",
      "FAILED",
      "CANCELLING",
      "CANCELLED",
    ];

    expect(nonDefaultOptionValues).toEqual(expectedStatuses);
    expect(LAUNCH_STATUSES).toEqual(expectedStatuses);
  });

  it("must include COMPLETED option and must NOT include RETRY_WAIT or SUCCEEDED", () => {
    renderComponent();

    const statusSelect = screen.getAllByRole("combobox")[0] as HTMLSelectElement;
    const optionValues = Array.from(statusSelect.options).map((opt) => opt.value);

    // Must include COMPLETED
    expect(optionValues).toContain("COMPLETED");

    const completedOption = Array.from(statusSelect.options).find(
      (opt) => opt.value === "COMPLETED"
    );
    expect(completedOption?.textContent).toContain("COMPLETED (已完成)");

    // Must NOT include RETRY_WAIT (Item status) or legacy SUCCEEDED
    expect(optionValues).not.toContain("RETRY_WAIT");
    expect(optionValues).not.toContain("SUCCEEDED");
  });

  it("triggers query with status=COMPLETED when COMPLETED is selected", async () => {
    renderComponent();

    const statusSelect = screen.getAllByRole("combobox")[0] as HTMLSelectElement;
    fireEvent.change(statusSelect, { target: { value: "COMPLETED" } });

    await waitFor(() => {
      expect(api.GET).toHaveBeenCalledWith(
        "/api/v1/experiment-launches",
        expect.objectContaining({
          params: expect.objectContaining({
            query: expect.objectContaining({
              status: "COMPLETED",
            }),
          }),
        })
      );
    });
  });

  it("clears status filter and resets API query when reset button is clicked", async () => {
    renderComponent();

    const statusSelect = screen.getAllByRole("combobox")[0] as HTMLSelectElement;
    fireEvent.change(statusSelect, { target: { value: "COMPLETED" } });

    // The reset button should appear when a filter is active
    const resetButton = await screen.findByRole("button", { name: "重置筛选" });
    expect(resetButton).toBeInTheDocument();

    fireEvent.click(resetButton);

    expect(statusSelect.value).toBe("");
    await waitFor(() => {
      expect(api.GET).toHaveBeenLastCalledWith(
        "/api/v1/experiment-launches",
        expect.objectContaining({
          params: expect.objectContaining({
            query: expect.objectContaining({
              status: undefined,
            }),
          }),
        })
      );
    });
  });
});
