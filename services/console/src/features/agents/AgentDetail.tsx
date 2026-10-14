import React, { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ArrowLeft, Bot, Calendar, ChevronRight, Layers, Plus, Trash2, User } from "lucide-react";
import { api } from "../../api/client";
import { queryKeys } from "../../api/query-keys";
import { formatApiError } from "../../api/errors";
import { CreateVersionDialog } from "./CreateVersionDialog";
import { DeleteAgentModal } from "./DeleteAgentModal";
import { EmptyState, ErrorState, LoadingState } from "../../components/StateViews";
import { Badge } from "../../components/Badge";
import { Button, PageHeader, Panel } from "../../components/ui/Primitives";
import { Modal } from "../../components/ui/Overlay";

export const AgentDetail: React.FC = () => {
  const { agentId } = useParams<{ agentId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [isDeleteOpen, setIsDeleteOpen] = useState(false);
  const [versionToArchive, setVersionToArchive] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const { data: agent, isLoading: isAgentLoading, error: agentError, refetch: refetchAgent } = useQuery({
    queryKey: queryKeys.agents.detail(agentId || ""),
    queryFn: async () => {
      if (!agentId) throw new Error("缺少 Agent ID");
      const res = await api.GET("/api/v1/agents", {
        params: { query: { id: agentId } },
      });
      if (res.error) throw res.error;
      const data = Array.isArray(res.data) ? res.data[0] : res.data;
      return data as import("../../api/schema").components["schemas"]["AgentResponse"];
    },
    enabled: Boolean(agentId),
  });

  const { data: versions, isLoading: isVersionsLoading, error: versionsError, refetch: refetchVersions } = useQuery({
    queryKey: queryKeys.agents.versions(agentId || ""),
    queryFn: async () => {
      if (!agentId) throw new Error("缺少 Agent ID");
      const res = await api.GET("/api/v1/agent-versions", {
        params: { query: { agent_id: agentId } },
      });
      if (res.error) throw res.error;
      return Array.isArray(res.data) ? res.data : [res.data];
    },
    enabled: Boolean(agentId),
  });

  const archiveMutation = useMutation({
    mutationFn: async (versionTag: string) => {
      if (!agentId) return;
      setActionError(null);
      const res = await api.POST("/api/v1/agent-versions/archive", {
        body: { agent_id: agentId, version: versionTag },
      });
      if (res.error) throw res.error;
      return res.data;
    },
    onSuccess: () => {
      if (agentId) {
        queryClient.invalidateQueries({ queryKey: queryKeys.agents.versions(agentId) });
        queryClient.invalidateQueries({ queryKey: queryKeys.agents.detail(agentId) });
        queryClient.invalidateQueries({ queryKey: queryKeys.agents.list() });
      }
    },
    onError: (err) => {
      setActionError(formatApiError(err));
    },
  });

  if (isAgentLoading) return <LoadingState message="正在加载 Agent 详情..." />;
  if (agentError) return <ErrorState message={formatApiError(agentError)} onRetry={() => refetchAgent()} />;
  if (!agent) return <ErrorState message="未找到对应的 Agent" />;

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <Link
          to="/agents"
          className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          <span>返回 Agent 列表</span>
        </Link>
        <PageHeader
          title={(
            <span className="flex min-w-0 items-center gap-2">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-primary-border bg-primary-subtle text-primary">
                <Bot className="h-4 w-4" />
              </span>
              <span className="truncate">{agent.name}</span>
              <Badge tone={agent.status.toUpperCase() === "ACTIVE" ? "pass" : "neutral"}>
                {agent.status.toUpperCase()}
              </Badge>
            </span>
          )}
          description={<span className="break-all font-mono">{agent.id}</span>}
          actions={(
            <>
              <Button variant="danger" onClick={() => setIsDeleteOpen(true)} title="删除该 Agent">
                <Trash2 className="h-4 w-4" />
                <span>删除 Agent</span>
              </Button>
              <Button onClick={() => setIsCreateOpen(true)}>
                <Plus className="h-4 w-4" />
                <span>创建新版本</span>
              </Button>
            </>
          )}
        />
      </div>

      {actionError && (
        <div className="p-3 text-xs bg-fail-subtle border border-fail-border rounded-lg text-fail font-medium">
          {actionError}
        </div>
      )}

      {/* Metadata Card */}
      <Panel className="grid grid-cols-1 gap-4 p-5 shadow-xs md:grid-cols-4">
        <div>
          <span className="text-xs font-medium text-muted-foreground block mb-1">负责人 / 团队</span>
          <span className="text-sm font-semibold text-foreground flex items-center gap-1.5">
            <User className="w-4 h-4 text-muted-foreground" />
            <span>{agent.owner || "未指定"}</span>
          </span>
        </div>
        <div>
          <span className="text-xs font-medium text-muted-foreground block mb-1">注册时间</span>
          <span className="text-sm font-semibold text-foreground flex items-center gap-1.5">
            <Calendar className="w-4 h-4 text-muted-foreground" />
            <span>{new Date(agent.created_at).toLocaleString("zh-CN", { hour12: false })}</span>
          </span>
        </div>
        <div>
          <span className="text-xs font-medium text-muted-foreground block mb-1">评测记录</span>
          <span className="text-sm font-semibold text-foreground flex items-center gap-2">
            <span>{agent.launch_count ?? 0} 次</span>
            {(agent.active_launch_count ?? 0) > 0 && (
              <Badge
                tone="running"
                title="活跃评测包含待执行、排队中、运行中、取消中等尚未结束状态的 Launch。"
                className="px-1.5 text-2xs"
              >
                {agent.active_launch_count} 条活跃评测
              </Badge>
            )}
          </span>
        </div>
        <div>
          <span className="text-xs font-medium text-muted-foreground block mb-1">详细描述</span>
          <span className="text-xs text-foreground-secondary line-clamp-2">
            {agent.description || "暂无描述"}
          </span>
        </div>
      </Panel>

      {/* Versions Section */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Layers className="w-4 h-4 text-primary" />
            <h3 className="text-base font-bold text-foreground">版本规格快照 (Agent Versions)</h3>
          </div>
          <span className="text-xs text-muted-foreground">所有版本规格创建后均为不可变快照</span>
        </div>

        {isVersionsLoading && <LoadingState message="正在加载版本记录..." />}
        {versionsError && <ErrorState message={formatApiError(versionsError)} onRetry={() => refetchVersions()} />}

        {!isVersionsLoading && !versionsError && versions && versions.length === 0 && (
          <EmptyState
            title="暂无任何版本"
            description="该 Agent 尚未创建任何版本规格。请点击上方按钮创建 1.0.0 版本。"
            action={
              <Button variant="primary" onClick={() => setIsCreateOpen(true)} className="text-xs">
                <Plus aria-hidden="true" className="w-3.5 h-3.5" />
                <span>立即创建版本</span>
              </Button>
            }
          />
        )}

        {!isVersionsLoading && !versionsError && versions && versions.length > 0 && (
          <div className="ui-panel overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm text-foreground-secondary">
                <thead className="bg-canvas/75 border-b border-border text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                  <tr>
                    <th className="px-6 py-3.5">版本号 (Tag)</th>
                    <th className="px-6 py-3.5">状态</th>
                    <th className="px-6 py-3.5">运行环境</th>
                    <th className="px-6 py-3.5">HTTP 调用端点</th>
                    <th className="px-6 py-3.5">创建时间</th>
                    <th className="px-6 py-3.5 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {versions.map((ver) => (
                    <tr key={ver.id} className="hover:bg-surface-muted/80 transition-colors">
                      <td className="px-6 py-4 font-mono font-bold text-foreground">
                        {ver.version}
                      </td>

                      <td className="px-6 py-4">
                        <Badge tone={ver.is_active ? "pass" : "neutral"}>
                          {ver.is_active ? "ACTIVE" : "ARCHIVED"}
                        </Badge>
                      </td>

                      <td className="px-6 py-4 text-xs">
                        {ver.environment ? (
                          <span className="px-2 py-0.5 rounded bg-primary-subtle text-primary-strong font-medium">
                            {ver.environment}
                          </span>
                        ) : (
                          <span className="text-muted-foreground">-</span>
                        )}
                      </td>

                      <td className="px-6 py-4 font-mono text-xs text-foreground-secondary max-w-xs truncate" title={ver.endpoint}>
                        {ver.endpoint}
                      </td>

                      <td className="px-6 py-4 text-xs text-muted-foreground">
                        {new Date(ver.created_at).toLocaleString("zh-CN", { hour12: false })}
                      </td>

                      <td className="px-6 py-4 text-right space-x-2">
                        <Link
                          to={`/agents/${agent.id}/versions/${ver.version}`}
                          // SC 2.5.8 Target Size (Minimum): the bare text link
                          // was 18px tall, below the 24px floor.
                          className="inline-flex min-h-7 items-center gap-0.5 px-1.5 text-xs font-semibold text-primary hover:text-primary-strong"
                        >
                          <span>查看配置</span>
                          <ChevronRight className="w-3 h-3" />
                        </Link>

                        {ver.is_active && (
                          <button
                            type="button"
                            onClick={() => setVersionToArchive(ver.version)}
                            disabled={archiveMutation.isPending}
                            // SC 2.5.8 Target Size (Minimum): the bare text link
                            // was 18px tall, below the 24px floor.
                            className="inline-flex min-h-7 items-center gap-1 px-2 text-xs font-semibold text-muted-foreground hover:text-fail transition-colors ml-2 cursor-pointer disabled:opacity-50"
                          >
                            <Archive className="w-3 h-3" />
                            <span>归档</span>
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {/* Create Version Dialog */}
      <CreateVersionDialog
        agentId={agent.id}
        isOpen={isCreateOpen}
        onClose={() => setIsCreateOpen(false)}
      />

      {/* Delete Agent Modal */}
      <DeleteAgentModal
        isOpen={isDeleteOpen}
        agent={{
          id: agent.id,
          name: agent.name,
          version_count: versions?.length ?? agent.version_count,
          launch_count: agent.launch_count,
          active_launch_count: agent.active_launch_count,
        }}
        onClose={() => setIsDeleteOpen(false)}
        onSuccess={() => navigate("/agents")}
      />

      <Modal
        open={versionToArchive !== null}
        onClose={() => setVersionToArchive(null)}
        title="归档版本"
        tone="danger"
        // A submitted archive must not be abandoned by a stray click.
        dismissable={!archiveMutation.isPending}
        footer={
          <>
            <Button
              type="button"
              variant="secondary"
              className="text-xs"
              onClick={() => setVersionToArchive(null)}
            >
              取消
            </Button>
            <Button
              type="button"
              variant="danger"
              className="text-xs"
              disabled={archiveMutation.isPending}
              onClick={() => {
                if (versionToArchive === null) return;
                archiveMutation.mutate(versionToArchive, {
                  onSuccess: () => setVersionToArchive(null),
                });
              }}
            >
              {archiveMutation.isPending ? "正在归档..." : "确认归档"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-foreground">
          确认归档版本{" "}
          <span className="font-semibold">{versionToArchive}</span> 吗？归档后该版本将不能用于新评测。
        </p>
      </Modal>
    </div>
  );
};
