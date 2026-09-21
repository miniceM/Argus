export const queryKeys = {
  agents: {
    all: ["agents"] as const,
    list: () => [...queryKeys.agents.all, "list"] as const,
    detail: (id: string) => [...queryKeys.agents.all, "detail", id] as const,
    versions: (agentId: string) => [...queryKeys.agents.all, "versions", agentId] as const,
    version: (agentId: string, version: string) => [...queryKeys.agents.all, "version", agentId, version] as const,
  },
  evaluators: {
    all: ["evaluators"] as const,
    list: () => [...queryKeys.evaluators.all, "list"] as const,
  },
  system: {
    info: () => ["system", "info"] as const,
  },
  launches: {
    all: ["launches"] as const,
    list: (filters?: Record<string, unknown>) => [...queryKeys.launches.all, "list", filters] as const,
    detail: (id: string) => [...queryKeys.launches.all, "detail", id] as const,
    items: (launchId: string) => [...queryKeys.launches.all, "items", launchId] as const,
    attempts: (itemExecutionId: string) => [...queryKeys.launches.all, "attempts", itemExecutionId] as const,
  },
};
