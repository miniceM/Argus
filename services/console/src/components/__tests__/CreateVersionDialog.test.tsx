import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CreateVersionDialog } from "../../features/agents/CreateVersionDialog";

vi.mock("../../api/client", () => ({
  api: {
    POST: vi.fn(),
  },
}));

describe("CreateVersionDialog Help Indicators (Issue #18)", () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  const renderDialog = () => {
    return render(
      <QueryClientProvider client={queryClient}>
        <CreateVersionDialog agentId="banking-agent" isOpen={true} onClose={vi.fn()} />
      </QueryClientProvider>
    );
  };

  it("renders all 6 required field help buttons on the form", () => {
    renderDialog();

    expect(screen.getByRole("button", { name: "查看「版本号 (Tag)」说明" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看「运行环境 (Environment)」说明" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看「远程调用端点 (Endpoint)」说明" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看「凭据引用 (SecretRef)」说明" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看「产物标识 (ArtifactRef)」说明" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看「请求映射关系 (Request Mapping)」说明" })).toBeInTheDocument();
  });

  it("opens popover with correct meaning and example when clicking help button", () => {
    renderDialog();

    const versionHelpBtn = screen.getByRole("button", { name: "查看「版本号 (Tag)」说明" });
    fireEvent.click(versionHelpBtn);

    // Verify content in popover
    expect(screen.getByText(/永久冻结不可修改/)).toBeInTheDocument();
    expect(screen.getByText(/1\.0\.0 或 v2/)).toBeInTheDocument();

    // Click secret help button
    const secretHelpBtn = screen.getByRole("button", { name: "查看「凭据引用 (SecretRef)」说明" });
    fireEvent.click(secretHelpBtn);

    expect(screen.getByText(/严禁直接填入明文 Secret/)).toBeInTheDocument();
    expect(screen.getByText("env://DEMO_AUTH_TOKEN")).toBeInTheDocument();
  });
});
