# Usage / Cost Evidence Contract

Argus records optional Usage / Cost evidence from the configured HTTP JSON Agent response. Agents remain ordinary business services; this feature adds no Langfuse or Argus Evaluation SDK dependency to the Agent and never calls a model or pricing service to fill missing data.

## AgentVersion configuration

Configure an explicit response mapping when the Agent returns reliable, invocation-total cost evidence:

```json
{
  "agent_id": "billing-agent",
  "version": "2.1.0",
  "endpoint": "https://agent.example/invoke",
  "usage_cost_mapping": {
    "input_tokens_path": "usage.input_tokens",
    "output_tokens_path": "usage.output_tokens",
    "total_tokens_path": "usage.total_tokens",
    "amount_path": "billing.cost",
    "currency_path": "billing.currency",
    "source": "provider_reported",
    "measurement_scope": "agent_invocation_total"
  }
}
```

Paths are dot-separated JSON object keys. Cost amount and currency paths must be configured together. `source` is restricted to `provider_reported` for this HTTP-response adapter, and the required measurement scope states that the reported amount covers the full Agent invocation (including any internal model/tool calls represented by that response). A sub-call amount must not be mapped as an invocation total. Each AgentVersion is immutable; the mapping is included in its digest and the Launch manifest.

## Missing and invalid evidence

- Missing amount or response is unknown, not zero. A literal numeric `0` is a valid recorded amount.
- Negative, non-finite, malformed amounts, currencies that do not normalize to three uppercase letters, and invalid token values are not accepted as valid evidence. Invalid usage fields are left unknown independently of an otherwise valid cost amount.
- Usage and cost are independent. Token-only evidence can be retained while cost remains unavailable; usage is never priced by Argus.
- For HTTP error responses, Argus preserves the existing execution/error behavior and extracts evidence only if the response is valid JSON and matches the mapping. A timeout or connection failure provides no evidence and is not assumed to be free.
- Raw response bodies and sensitive headers are not copied into the cost evidence record.

## Retry and aggregation semantics

The frozen policy is `case-cost-v1` / `launch_case_total`:

- A Case's cost includes every execution Attempt for that Item in the same Launch, across all `dispatch_generation` values (including automatic retries and later Retry Failed / Resume attempts).
- If any Attempt for that Launch/Case across any generation has unknown or invalid cost, the Case does not have a complete cost. Known Attempt evidence remains in the Snapshot for audit but is not presented as the complete Case amount.
- A later Retry Failed / Resume generation is a new result revision. Its new Snapshot includes the earlier same-Launch attempts needed to obtain the final Case result, while prior Snapshots remain unchanged. Attempts from other Launches are never included.
- `cost_coverage` is complete-cost Case count divided by all Cases in that Run/cohort. `total_cost` sums only complete, mutually compatible Cases; under partial coverage it is not the full-run bill. `cost_per_case` divides that sum by the number of complete-cost Cases and must always be read with coverage.
- Missing costs are never filled with zero. Runs with no cost evidence expose `COST_NOT_RECORDED`; partial coverage exposes `PARTIAL_COST_COVERAGE` when a valid partial mean is available.
- Argus does not convert currencies or combine currencies, incompatible sources, measurement scopes, or policy versions.

## Comparison and history

Cost deltas use the quality-comparable Case cohort already selected by the frozen comparison. A delta is emitted only when both sides have complete cost coverage for every Case in that cohort, and currency, source, scope, and policy are compatible. Otherwise the API returns `status=NOT_COMPARABLE`, a stable reason, and coverage; the Console shows an em dash for delta and explains the reason.

Per-Case Usage / Cost evidence, Attempt evidence, the aggregation policy version, and calculated Run metrics are included in the immutable `RunResultSnapshot`. Historical Snapshot reads and comparisons use only those frozen values; they do not query current Agent settings, external traces, or changing price tables.
