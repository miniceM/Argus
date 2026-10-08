import { createBrowserRouter, Navigate } from "react-router-dom";
import { AppShell } from "../components/AppShell";
import { AgentsList } from "../features/agents/AgentsList";
import { AgentDetail } from "../features/agents/AgentDetail";
import { AgentVersionDetail } from "../features/agents/AgentVersionDetail";
import { LaunchesList } from "../features/launches/LaunchesList";
import { CreateLaunch } from "../features/launches/CreateLaunch";
import { LaunchDetail } from "../features/launches/LaunchDetail";
import { CredentialsPage } from "../features/credentials/CredentialsPage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppShell />,
    children: [
      { path: "credentials", element: <CredentialsPage /> },
      {
        index: true,
        element: <Navigate to="/launches" replace />,
      },
      {
        path: "experiments",
        element: <Navigate to="/launches" replace />,
      },
      {
        path: "agents",
        element: <AgentsList />,
      },
      {
        path: "agents/:agentId",
        element: <AgentDetail />,
      },
      {
        path: "agents/:agentId/versions/:version",
        element: <AgentVersionDetail />,
      },
      {
        path: "launches",
        element: <LaunchesList />,
      },
      {
        path: "launches/new",
        element: <CreateLaunch />,
      },
      {
        path: "launches/:launchId",
        element: <LaunchDetail />,
      },
      {
        path: "*",
        element: (
          <div className="py-20 text-center space-y-3">
            <h2 className="text-xl font-bold text-foreground">404 - 页面未找到</h2>
            <p className="text-xs text-foreground-secondary">请求的页面不存在或已被移除。</p>
            <a
              href="/launches"
              className="inline-block px-4 py-2 text-xs font-semibold text-foreground-inverse bg-primary rounded-lg hover:bg-primary-hover"
            >
              返回评测列表
            </a>
          </div>
        ),
      },
    ],
  },
]);
