import React, { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { Bot, ExternalLink, FlaskConical, Layers, Menu, ShieldCheck } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { queryKeys } from "../api/query-keys";
import { Button } from "./ui/Primitives";
import { SideDrawer } from "./ui/Overlay";

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

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `group flex min-h-9 items-center gap-2.5 rounded-md px-3 text-sm transition-colors ${
    isActive
      ? "bg-surface-muted font-semibold text-foreground"
      : "text-foreground-secondary hover:bg-surface-muted hover:text-foreground"
  }`;

interface ShellNavProps {
  dashboardUrl: string | null;
  dashboardStatus: string;
  /** Called after a navigation so the narrow-viewport drawer closes. */
  onNavigate?: () => void;
}

/**
 * The navigation content is rendered by exactly one host at a time: the static aside on
 * wide viewports, or the drawer on narrow ones. Keeping one component guarantees the two
 * never drift apart.
 */
const ShellNav: React.FC<ShellNavProps> = ({ dashboardUrl, dashboardStatus, onNavigate }) => (
  <>
    <nav aria-label="主导航" className="flex-1 space-y-0.5 overflow-y-auto px-2.5 py-3">
      <NavLink to="/agents" className={navLinkClass} onClick={onNavigate}>
        <Bot aria-hidden="true" className="size-4 shrink-0 text-muted-foreground group-hover:text-current" />
        <span>Agents</span>
      </NavLink>
      <NavLink to="/launches" className={navLinkClass} onClick={onNavigate}>
        <FlaskConical aria-hidden="true" className="size-4 shrink-0 text-muted-foreground group-hover:text-current" />
        <span>Launches</span>
      </NavLink>
    </nav>

    <div className="border-t border-border p-2.5">
      {dashboardUrl ? (
        <a
          href={dashboardUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="flex min-h-9 items-center justify-between gap-2 rounded-md px-2.5 text-xs text-foreground-secondary transition-colors hover:bg-surface-muted hover:text-foreground"
        >
          <span className="flex min-w-0 items-center gap-2">
            <Layers aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="truncate">Langfuse Dashboard</span>
          </span>
          <ExternalLink aria-hidden="true" className="size-3 shrink-0 text-muted-foreground" />
        </a>
      ) : (
        <span
          aria-disabled="true"
          aria-live="polite"
          className="flex min-h-9 items-center gap-2 px-2.5 text-micro leading-4 text-muted-foreground"
        >
          <Layers aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
          <span>{dashboardStatus}</span>
        </span>
      )}
    </div>
  </>
);

export const AppShell: React.FC = () => {
  const { data: sysInfo, isPending, isError } = useQuery({
    queryKey: queryKeys.system.info(),
    queryFn: async () => {
      const res = await api.GET("/api/v1/system/info");
      if (res.error || !res.data) throw new Error("Failed to load system information");
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

  // Narrow viewports cannot spare the fixed sidebar width, so the navigation collapses
  // into a drawer and the content column keeps the full width.
  const [navOpen, setNavOpen] = useState(false);
  const closeNav = useCallback(() => setNavOpen(false), []);
  const location = useLocation();
  const navTriggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeNav();
  }, [location.pathname, closeNav]);

  return (
    <div className="flex h-screen min-h-0 overflow-hidden bg-canvas text-foreground">
      {/* Wide viewports: the fixed sidebar keeps its current layout. */}
      <aside className="hidden w-sidebar shrink-0 flex-col border-r border-border bg-surface lg:flex">
        <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-border px-4">
          <div className="flex size-7 items-center justify-center rounded-md bg-primary-subtle text-primary">
            <ShieldCheck aria-hidden="true" className="size-4" />
          </div>
          <div className="min-w-0 leading-tight">
            <span className="block text-sm font-semibold tracking-tight text-foreground">Argus</span>
            <span className="block text-2xs text-muted-foreground">Control Plane</span>
          </div>
        </div>
        <ShellNav dashboardUrl={dashboardUrl} dashboardStatus={dashboardStatus ?? ""} />
      </aside>

      {/* Narrow viewports: the same navigation as an overlay drawer. */}
      <SideDrawer
        open={navOpen}
        onClose={closeNav}
        returnFocusRef={navTriggerRef}
        title="主导航"
        subtitle="Argus · Control Plane"
        icon={(
          <div className="flex size-7 items-center justify-center rounded-md bg-primary-subtle text-primary">
            <ShieldCheck aria-hidden="true" className="size-4" />
          </div>
        )}
        drawerSide="left"
        drawerSize="sidebar"
      >
        <div className="flex min-h-full flex-col">
          <ShellNav dashboardUrl={dashboardUrl} dashboardStatus={dashboardStatus ?? ""} onNavigate={closeNav} />
        </div>
      </SideDrawer>

      <div
        className="flex min-w-0 flex-1 flex-col overflow-hidden"
        aria-hidden={navOpen || undefined}
        inert={navOpen}
      >
        <header className="z-10 flex h-14 shrink-0 items-center justify-between gap-4 border-b border-border bg-surface px-5">
          <div className="flex min-w-0 items-center gap-3">
            <Button
              type="button"
              variant="secondary"
              aria-label="打开导航"
              aria-expanded={navOpen}
              ref={navTriggerRef}
              className="min-h-9 px-2 lg:hidden"
              onClick={() => setNavOpen(true)}
            >
              <Menu aria-hidden="true" className="size-4" />
            </Button>
            <div className="min-w-0 truncate text-sm font-medium text-foreground-secondary">
              Agent Evaluation Control Plane
            </div>
          </div>
          {sysInfo && (
            <div className="flex shrink-0 items-center gap-3 text-micro text-muted-foreground">
              <span className="rounded border border-border bg-surface-muted px-1.5 py-0.5 font-medium text-foreground-secondary">
                {sysInfo.environment.toUpperCase()}
              </span>
              <span className="hidden font-mono sm:inline">
                Build: <span className="font-semibold text-foreground-secondary">{sysInfo.build_id.slice(0, 8)}</span>
              </span>
              <span className="font-mono">v{sysInfo.version}</span>
            </div>
          )}
        </header>

        <main className="min-h-0 flex-1 overflow-y-auto bg-canvas p-5 sm:p-6">
          <div className="mx-auto w-full max-w-content">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
};
