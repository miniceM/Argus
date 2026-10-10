import { describe, it, expect, vi, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ResultSnapshotPanel, evidenceTone } from "../resultSnapshot";
import { api } from "../../../api/client";
import { snapshotListKey } from "../launchSnapshotQueries";

vi.mock("../../../api/client", () => ({ api: { GET: vi.fn(), POST: vi.fn() } }));

type Rev = Record<string, unknown>;

const revision = (over: Partial<Rev> = {}): Rev => ({
  snapshot_id: "snap-1",
  revision: 1,
  created_at: "2026-09-30T00:00:00Z",
  source_result_digest: "abcdef0123456789abcdef0123456789",
  manifest_digest: "manifest-digest",
  evidence_state: "COMPLETE",
  evidence_reasons: [],
  total_cases: 6,
  quality_pass_count: 6,
  quality_fail_count: 0,
  quality_unknown_count: 0,
  is_latest: true,
  ...over,
});

const list = (revisions: Rev[]) => ({
  launch_id: "launch-85",
  latest_snapshot_id: revisions[0]?.snapshot_id ?? null,
  latest_revision: revisions[0]?.revision ?? null,
  revisions,
});

const detail = (rev: Rev) => ({
  launch_id: "launch-85",
  snapshot_id: rev.snapshot_id,
  revision: rev.revision,
  created_at: rev.created_at,
  source_result_digest: rev.source_result_digest,
  manifest_digest: rev.manifest_digest,
  evidence_state: rev.evidence_state,
  evidence_reasons: rev.evidence_reasons,
  releasable: rev.evidence_state === "COMPLETE",
  versions: {},
  summary: {},
  items: [],
});

function renderPanel(
  props: Partial<React.ComponentProps<typeof ResultSnapshotPanel>> = {},
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  return render(
    <QueryClientProvider client={client}>
      <ResultSnapshotPanel
        launchId="launch-85"
        selectedSnapshotId={null}
        onSelect={() => {}}
        {...props}
      />
    </QueryClientProvider>,
  );
}

const mockApi = (revisions: Rev[]) => {
  (api.GET as any).mockImplementation((path: string) => {
    if (path.endsWith("/result-snapshots")) {
      return Promise.resolve({ data: list(revisions) });
    }
    if (path.includes("/result-snapshots/")) {
      const id = path.split("/").pop();
      const found = revisions.find((r) => r.snapshot_id === id) ?? revisions[0];
      return Promise.resolve({ data: detail(found) });
    }
    return Promise.resolve({ data: null });
  });
};

describe("Issue #85 ResultSnapshotPanel", () => {
  afterEach(() => vi.clearAllMocks());

  it("maps evidence state to a badge tone where only COMPLETE is a pass", () => {
    expect(evidenceTone("COMPLETE")).toBe("pass");
    // A diagnostic snapshot is not a product failure, so it must not read red.
    expect(evidenceTone("DIAGNOSTIC")).toBe("neutral");
  });

  it("says plainly when no revision has been frozen yet", async () => {
    mockApi([]);
    renderPanel();
    expect(await screen.findByTestId("result-snapshot-empty")).toBeInTheDocument();
    expect(screen.getByTestId("result-snapshot-empty")).toHaveTextContent("尚未冻结任何结果版本");
  });

  it("names the revision, its digest and its shareable fixed link", async () => {
    mockApi([revision()]);
    renderPanel();
    expect(await screen.findByTestId("snapshot-revision")).toHaveTextContent("Revision 1");
    expect(screen.getByTestId("snapshot-latest-tag")).toBeInTheDocument();
    expect(screen.getByTestId("snapshot-source-digest")).toHaveTextContent("abcdef0123456789");
    // The link pins a concrete snapshot id; it never says "latest".
    const link = screen.getByTestId("snapshot-share-url");
    expect(link).toHaveTextContent("snapshot_id=snap-1");
    expect(link).not.toHaveTextContent("latest");
    expect(screen.getByTestId("snapshot-evidence-badge")).toHaveTextContent("证据完整");
  });

  it("keeps cached revisions visible after a background history refresh fails", async () => {
    const revisions = [
      revision({ snapshot_id: "snap-2", revision: 2, is_latest: true }),
      revision({ snapshot_id: "snap-1", revision: 1, is_latest: false }),
    ];
    const cachedList = list(revisions);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    client.setQueryData(snapshotListKey("launch-85"), cachedList);
    let failRefresh = true;
    (api.GET as any).mockImplementation((path: string) => {
      if (path.endsWith("/result-snapshots")) {
        return failRefresh
          ? Promise.resolve({ error: { detail: "history temporarily unavailable" }, response: { status: 503 } })
          : Promise.resolve({ data: cachedList });
      }
      if (path.includes("/result-snapshots/")) {
        return Promise.resolve({ data: detail(revisions[0]) });
      }
      return Promise.resolve({ data: null });
    });

    renderPanel({ selectedSnapshotId: "snap-1" }, client);

    expect(await screen.findByTestId("snapshot-history-refresh-error")).toBeInTheDocument();
    expect(screen.getByTestId("snapshot-revision")).toHaveTextContent("Revision 1");
    expect(screen.getByTestId("snapshot-history")).toBeInTheDocument();
    expect(screen.getByTestId("snapshot-newer-available")).toHaveTextContent("Revision 2");

    failRefresh = false;
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => {
      expect(screen.queryByTestId("snapshot-history-refresh-error")).not.toBeInTheDocument();
    });
    expect(screen.getByTestId("snapshot-revision")).toHaveTextContent("Revision 1");
  });

  it("shows the full error state when the first revision-directory request fails", async () => {
    (api.GET as any).mockResolvedValue({ error: { detail: "history unavailable" }, response: { status: 503 } });
    renderPanel();

    expect(await screen.findByText("加载快照版本失败")).toBeInTheDocument();
    expect(screen.queryByTestId("result-snapshot-empty")).not.toBeInTheDocument();
  });

  it("marks an incomplete revision as diagnostic and lists why", async () => {
    mockApi([
      revision({
        evidence_state: "DIAGNOSTIC",
        evidence_reasons: ["1/6 个用例评测失败或未产出结果"],
        quality_unknown_count: 1,
      }),
    ]);
    renderPanel();
    expect(await screen.findByTestId("snapshot-evidence-badge")).toHaveTextContent("诊断快照");
    const reasons = screen.getByTestId("snapshot-evidence-reasons");
    expect(reasons).toHaveTextContent("1/6 个用例评测失败或未产出结果");
    expect(screen.getByTestId("snapshot-evidence-help")).toHaveTextContent("不可作为正式 Baseline");
    await waitFor(() => {
      expect(screen.getByTestId("snapshot-not-releasable")).toBeInTheDocument();
    });
  });

  it("warns that a newer revision exists while viewing a historical one", async () => {
    mockApi([
      revision({ snapshot_id: "snap-2", revision: 2, is_latest: true }),
      revision({ snapshot_id: "snap-1", revision: 1, is_latest: false }),
    ]);
    renderPanel({ selectedSnapshotId: "snap-1" });
    expect(await screen.findByTestId("snapshot-revision")).toHaveTextContent("Revision 1");
    expect(screen.getByTestId("snapshot-newer-available")).toHaveTextContent("已有更新的 Revision 2");
  });

  it("offers every revision in the history so the user can switch", async () => {
    mockApi([
      revision({ snapshot_id: "snap-2", revision: 2, is_latest: true }),
      revision({ snapshot_id: "snap-1", revision: 1, is_latest: false }),
    ]);
    const onSelect = vi.fn();
    renderPanel({ onSelect });
    const history = await screen.findByTestId("snapshot-history");
    expect(history).toBeInTheDocument();
    const older = screen.getByTestId("snapshot-revision-1");
    expect(older).toBeInTheDocument();
    older.click();
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("snap-1"));
  });

  it("does not render a history switcher for a single revision", async () => {
    mockApi([revision()]);
    renderPanel();
    await screen.findByTestId("snapshot-revision");
    expect(screen.queryByTestId("snapshot-history")).not.toBeInTheDocument();
  });
});
