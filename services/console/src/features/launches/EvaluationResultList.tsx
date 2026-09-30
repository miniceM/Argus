import React from "react";
import { Badge } from "../../components/Badge";
import { Panel } from "../../components/ui/Primitives";
import {
  formatResultValue,
  isNumericValue,
  resultErrorReason,
  resultStatusMeta,
  resultTypeLabel,
  type EvaluationResult,
} from "./evaluationResults";

/**
 * Issue #82 — render typed, explainable evaluation results.
 *
 * `variant="compact"` is a one-line-per-metric summary for the item table;
 * `variant="detail"` adds the type, status, explanation, evidence, normalized
 * value and duration needed to audit a single case.
 */

function valueToneClass(result: EvaluationResult): string {
  if (result.status !== "succeeded") return "text-muted-foreground";
  if (!isNumericValue(result)) return "text-foreground";
  const numeric = result.value as number;
  if (numeric >= 1) return "text-pass-strong";
  if (numeric > 0) return "text-timeout";
  return "text-fail";
}

const CompactResult: React.FC<{ result: EvaluationResult }> = ({ result }) => {
  const status = resultStatusMeta(result.status);
  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-micro font-mono bg-surface-muted text-foreground-secondary border border-border"
      data-testid={`typed-result-${result.evaluator_id}`}
      data-result-type={result.result_type}
      data-result-status={result.status}
    >
      <span className="text-muted-foreground">{result.evaluator_id}:</span>
      <span className={`font-semibold ${valueToneClass(result)}`}>
        {formatResultValue(result)}
      </span>
      <span className="text-muted-foreground">({resultTypeLabel(result.result_type)})</span>
      {result.status !== "succeeded" && (
        <Badge tone={status.tone} className="text-micro">
          {status.label}
        </Badge>
      )}
    </span>
  );
};

const DetailResult: React.FC<{ result: EvaluationResult }> = ({ result }) => {
  const status = resultStatusMeta(result.status);
  const reason = resultErrorReason(result);
  return (
    <div
      className="rounded border border-border bg-surface-muted p-3 space-y-2"
      data-testid={`typed-result-${result.evaluator_id}`}
      data-result-type={result.result_type}
      data-result-status={result.status}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-sm font-semibold text-foreground">
          {result.evaluator_id}
        </span>
        <span className="flex items-center gap-1.5">
          <Badge tone="neutral" className="text-micro">
            {resultTypeLabel(result.result_type)}
          </Badge>
          <Badge tone={status.tone} className="text-micro">
            {status.label}
          </Badge>
        </span>
      </div>

      <div className="flex items-baseline gap-2">
        <span className="text-micro text-muted-foreground">取值</span>
        <span
          className={`font-mono text-sm font-semibold ${valueToneClass(result)}`}
          data-testid={`typed-value-${result.evaluator_id}`}
        >
          {formatResultValue(result)}
        </span>
        {result.evaluator_version && (
          <span className="text-micro text-muted-foreground font-mono">
            @{result.evaluator_version}
          </span>
        )}
      </div>

      {result.normalized_value !== null && result.normalized_value !== undefined && (
        <div className="flex items-baseline gap-2 text-micro text-muted-foreground">
          <span>归一化值</span>
          <span className="font-mono">{result.normalized_value}</span>
        </div>
      )}

      {result.comment && (
        <p className="text-xs text-foreground-secondary break-words">{result.comment}</p>
      )}

      {reason && (
        <p
          className="text-xs text-fail break-words"
          data-testid={`typed-reason-${result.evaluator_id}`}
        >
          {reason}
        </p>
      )}

      {result.evidence && Object.keys(result.evidence).length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">证据 (Evidence)</summary>
          <pre className="mt-1 overflow-x-auto rounded bg-surface p-2 font-mono text-micro text-foreground-secondary">
            {JSON.stringify(result.evidence, null, 2)}
          </pre>
        </details>
      )}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-micro text-muted-foreground font-mono">
        {result.duration_ms !== null && result.duration_ms !== undefined && (
          <span>耗时 {result.duration_ms} ms</span>
        )}
        {result.provenance?.binding_id && (
          <span title="冻结绑定">绑定 {result.provenance.binding_id}</span>
        )}
        {result.provenance?.contract_status && (
          <span title="冻结契约状态">契约 {result.provenance.contract_status}</span>
        )}
      </div>
    </div>
  );
};

export const EvaluationResultList: React.FC<{
  results?: EvaluationResult[] | null;
  variant?: "compact" | "detail";
  emptyLabel?: string;
}> = ({ results, variant = "compact", emptyLabel = "该用例尚未产生评测结果" }) => {
  if (!results || results.length === 0) {
    return <span className="text-xs text-muted-foreground">{emptyLabel}</span>;
  }

  if (variant === "detail") {
    return (
      <div className="space-y-3">
        {results.map((result) => (
          <DetailResult key={result.evaluator_id} result={result} />
        ))}
      </div>
    );
  }

  return (
    <div className="flex flex-wrap gap-1.5">
      {results.map((result) => (
        <CompactResult key={result.evaluator_id} result={result} />
      ))}
    </div>
  );
};

export const EvaluationResultPanel: React.FC<{
  results?: EvaluationResult[] | null;
  traceUrl?: string | null;
}> = ({ results, traceUrl }) => (
  <Panel title="评测结果 (Evaluation Results)" className="p-4">
    <EvaluationResultList results={results} variant="detail" />
    {traceUrl && (
      <a
        href={traceUrl}
        target="_blank"
        rel="noreferrer"
        className="mt-3 inline-block text-xs text-primary underline"
      >
        在 Langfuse 中查看 Trace
      </a>
    )}
  </Panel>
);
