import React from "react";
import { NavLink, Outlet } from "react-router-dom";
import { Bot, ExternalLink, FlaskConical, Layers, ShieldCheck } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { queryKeys } from "../api/query-keys";

const isSafeDashboardUrl = (value: unknown): value is string => {
  if (typeof value !== "string" || !value || /[\u0000-\u0020\u007f\\?#]/.test(value)) {
    return false;
  }

  try {
    const parsed = new URL(value);
    return (
      (parsed.protocol === "http:" || parsed.protocol === "https:") &&
      Boolean(parsed.hostname) &&
      !parsed.username &&
      !parsed.password
    );
  } catch {
    return false;
  }
};

export const AppShell: React.FC = () => {
  const { data: sysInfo, isPending, isError } = useQuery({
    queryKey: queryKeys.system.info(),
    queryFn: async () => {
      const res = await api.GET("/api/v1/system/info");
      if (res.error || !res.data) {
        throw new Error("Failed to load system information");
      }
      return res.data;
    },
    staleTime: 60000,
  });
  const configuredDashboardUrl = sysInfo?.langfuse_dashboard_url;
  const dashboardUrl = isSafeDashboardUrl(configuredDashboardUrl) ? configuredDashboardUrl : null;
  const dashboardStatus = dashboardUrl
    ? null
    : configuredDashboardUrl != null
      ? "Langfuse Dashboard 地址无效"
      : sysInfo
        ? "未配置 Langfuse Dashboard"
        : isError
          ? "无法获取 Langfuse Dashboard 地址"
          : isPending
            ? "正在加载 Langfuse Dashboard 地址"
            : "无法获取 Langfuse Dashboard 地址";

  return (
    <div className="flex h-screen bg-slate-50 overflow-hidden font-sans text-slate-800">
      {/* Sidebar */}
      <aside className="w-64 bg-slate-900 text-slate-300 flex flex-col flex-shrink-0 border-r border-slate-800">
        {/* Brand */}
        <div className="h-16 flex items-center px-6 gap-3 border-b border-slate-800">
          <div className="w-8 h-8 rounded-lg bg-indigo-600 flex items-center justify-center text-white font-bold shadow-sm">
            <ShieldCheck className="w-5 h-5" />
          </div>
          <div>
            <span className="font-bold text-base text-white tracking-tight">Argus</span>
            <span className="text-xs text-indigo-400 block font-mono">Control Plane</span>
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 px-4 py-6 space-y-1.5 overflow-y-auto">
          <NavLink
            to="/agents"
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                isActive
                  ? "bg-indigo-600 text-white shadow-sm"
                  : "text-slate-400 hover:bg-slate-800 hover:text-slate-200"
              }`
            }
          >
            <Bot className="w-4 h-4" />
            <span>Agents</span>
          </NavLink>

          <NavLink
            to="/launches"
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                isActive
                  ? "bg-indigo-600 text-white shadow-sm"
                  : "text-slate-400 hover:bg-slate-800 hover:text-slate-200"
              }`
            }
          >
            <FlaskConical className="w-4 h-4" />
            <span>Launches</span>
          </NavLink>
        </nav>

        {/* External System Link */}
        <div className="p-4 border-t border-slate-800 bg-slate-950/40">
          {dashboardUrl ? (
            <a
              href={dashboardUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center justify-between px-3 py-2 rounded text-xs font-medium text-slate-400 hover:text-slate-200 hover:bg-slate-800/60 transition-colors"
            >
              <span className="flex items-center gap-2">
                <Layers className="w-3.5 h-3.5 text-indigo-400" />
                <span>Langfuse Dashboard</span>
              </span>
              <ExternalLink className="w-3 h-3 text-slate-500" />
            </a>
          ) : (
            <span
              aria-disabled="true"
              aria-live={isPending && !sysInfo ? "polite" : undefined}
              className="flex items-center gap-2 px-3 py-2 rounded text-xs font-medium text-slate-500 cursor-not-allowed"
            >
              <Layers className="w-3.5 h-3.5 text-slate-500" />
              <span>{dashboardStatus}</span>
            </span>
          )}
        </div>
      </aside>

      {/* Main Content Area */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {/* Header */}
        <header className="h-16 bg-white border-b border-slate-200 flex items-center justify-between px-8 z-10 flex-shrink-0 shadow-xs">
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-bold text-slate-900 tracking-tight">Agent Evaluation Control Plane</h1>
          </div>

          <div className="flex items-center gap-3 text-xs">
            {sysInfo && (
              <>
                <span className="inline-flex items-center px-2 py-0.5 rounded-full font-medium bg-indigo-50 text-indigo-700 border border-indigo-200">
                  {sysInfo.environment.toUpperCase()}
                </span>
                <span className="text-slate-500 font-mono">
                  Build: <span className="font-semibold text-slate-700">{sysInfo.build_id.slice(0, 8)}</span>
                </span>
                <span className="text-slate-300">|</span>
                <span className="text-slate-500 font-mono">
                  v{sysInfo.version}
                </span>
              </>
            )}
          </div>
        </header>

        {/* Main Body */}
        <main className="flex-1 overflow-y-auto p-8 bg-slate-50">
          <div className="max-w-7xl mx-auto">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
};
