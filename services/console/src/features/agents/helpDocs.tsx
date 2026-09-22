import { FieldHelpProps } from "../../components/FieldHelp";

export const AGENT_VERSION_FIELD_HELPS: Record<string, Omit<FieldHelpProps, "placement" | "className">> = {
  version: {
    title: "版本号 (Tag)",
    meaning:
      "定义该 Agent 规格快照的唯一版本标识。创建后平台会计算 SHA-256 摘要并永久冻结不可修改，保证后续发起的评测实验具备绝对的可重现性与合规审计能力。",
    rules: "必填，同一 Agent 下版本号不可重复。若需变更配置，请创建新的版本快照。",
    example: "1.0.0 或 v2",
  },
  environment: {
    title: "运行环境 (Environment)",
    meaning:
      "标记该 Agent 实例当前部署的运行环境，便于在管理列表与评测报告中快速筛选、归类以及横向对比不同环境下的表现。",
    rules: "选填。建议使用团队约定的标准环境代号（如 staging、test、production 等）。",
    example: "staging / test / production / local",
  },
  endpoint: {
    title: "远程调用端点 (Endpoint)",
    meaning:
      "Argus Eval Runner 发起评测时执行 HTTP POST 请求的 Agent 实际网络接口地址。Runner 会在此端点上传播标准 W3C traceparent 链路追踪头。",
    rules: (
      <ul className="list-disc list-inside space-y-0.5">
        <li>
          必须使用 <code className="font-mono bg-slate-100 px-1 py-0.5 rounded">http://</code> 或{" "}
          <code className="font-mono bg-slate-100 px-1 py-0.5 rounded">https://</code> 协议；
        </li>
        <li>
          <strong className="text-rose-600">严禁在 URL 中硬编码密码</strong>（如{" "}
          <code className="font-mono text-slate-500">http://user:pwd@host</code>）；
        </li>
        <li>
          <strong className="text-rose-600">禁止在 Query 中携带敏感参数</strong>（如{" "}
          <code className="font-mono text-slate-500">?token=...</code>），鉴权请使用 SecretRef。
        </li>
      </ul>
    ),
    example: "http://demo-agent-v1:8080/invoke 或 https://agent.internal/api/v1/chat",
  },
  credentialRef: {
    title: "凭据引用 (SecretRef)",
    meaning:
      "被测 Agent 接口若需要 Token 鉴权，在此配置密钥的引用标识。Runner 会在运行时动态读取并注入请求头，同时在上报 Langfuse Trace 时自动打码脱敏。",
    rules: (
      <span>
        <strong className="text-rose-600">严禁直接填入明文 Secret</strong>。当前版本支持{" "}
        <code className="font-mono bg-slate-100 px-1 py-0.5 rounded">env://&lt;环境变量名&gt;</code> 格式（读取宿主白名单内的环境变量）。无需鉴权请留空。
      </span>
    ),
    example: "env://DEMO_AUTH_TOKEN",
  },
  artifactRef: {
    title: "产物标识 (ArtifactRef)",
    meaning:
      "将当前的 AgentVersion 规格与研发 CI/CD 流水线中实际构建出的代码版本或容器镜像关联，实现评测结果的精准溯源与实验复现。",
    rules: "选填。支持填写 Git 提交 SHA、Release 标签或 Docker 镜像 Digest。",
    example: "git:a1b2c3d 或 sha256:7f8e...",
  },
  requestMapping: {
    title: "请求映射关系 (Request Mapping)",
    meaning:
      "业务 Agent 保持原生的 HTTP API 契约（零 Eval SDK 侵入）。Request Mapping 充当适配器，在调用时自动从评测集 Item 的数据中提取指定字段，拼装为 Agent 期望的入参格式。",
    rules: (
      <span>
        必须为合法 JSON 字典。点路径语法格式：
        <code className="block mt-1 font-mono bg-slate-100 p-1.5 rounded text-slate-700">
          &#123;&quot;&lt;Agent参数名&gt;&quot;: &quot;input.&lt;评测集字段路径&gt;&quot;&#125;
        </code>
      </span>
    ),
    example: '{"messages": "input.messages", "customer_id": "input.customer_id"}',
  },
};
