"""纯配置测试工厂；不把合成 profile 当作真实资格证据。"""


def profile_payload(profile_id="native-otel-v1", **changes):
    fields = {
        name: [[physical]]
        for name, physical in {
            "observation_id": "id",
            "trace_id": "traceId",
            "parent_step_id": "parentObservationId",
            "started_at": "startTime",
            "ended_at": "endTime",
            "provider_type": "type",
            "raw_name": "name",
            "input": "input",
            "output": "output",
        }.items()
    }
    fields.update(
        {
            name: [["metadata", "attributes.workflow." + physical]]
            for name, physical in {
                "phase": "phase",
                "status": "status",
                "workflow_scope_id": "scope_id",
                "event_id": "event_id",
                "sequence": "sequence",
                "call_id": "call_id",
                "duration_ms": "duration_ms",
                "argument_visibility": "arguments_visibility",
                "hidden_argument_paths": "hidden_paths",
                "causal_event_ids": "predecessor_event_ids",
                "terminal_event_count": "event_count",
                "terminal_event_ids": "event_ids",
                "terminal_final_sequence": "final_sequence",
            }.items()
        }
    )
    value = {
        "profile_id": profile_id,
        "version": "fixture-1",
        "provider": "langfuse",
        "api_version": "v2",
        "provider_major_version": 4,
        "fields": fields,
        "metadata_equals": {"attributes.workflow.source": "business-instrumentation-v1"},
        "type_map": {"SPAN": "CUSTOM", "TOOL": "TOOL", "GENERATION": "LLM", "CHAIN": "AGENT"},
        "phase_map": {"REQUESTED": "REQUESTED", "EXECUTED": "EXECUTED", "BLOCKED": "BLOCKED", "TERMINAL": "EVENT"},
        "status_map": {"SUCCEEDED": "SUCCEEDED", "FAILED": "FAILED", "CANCELLED": "CANCELLED"},
        "safe_names": {"workflow.terminal": "workflow.terminal", "remote-agent-http": "remote-agent-http"},
        "capabilities": {
            "tool_executions": "COMPLETE",
            "tool_requests": "COMPLETE",
            "tool_arguments": "COMPLETE",
            "human_events": "NONE",
            "ordering": "COMPLETE",
            "timing": "PARTIAL",
        },
        "completion_protocol": "workflow-inventory-v1",
        "human_authority_id": None,
    }
    return value | changes


def contract_payload(**changes):
    value = {
        "contract_version": "1.0",
        "schema_version": "1.0",
        "assembler_version": "fixture-1",
        "source_profile_set": [profile_payload(), profile_payload("pydantic-ai-v1")],
        "source_profile_selector_version": "agent-version-declaration-v1",
        "tool_mapping": {
            "version": "fixture-1",
            "entries": [
                {
                    "source_profile_id": "native-otel-v1",
                    "source_profile_version": "fixture-1",
                    "raw_name": "crm_lookup",
                    "tool_identity": "banking.customer.lookup",
                },
                {
                    "source_profile_id": "pydantic-ai-v1",
                    "source_profile_version": "fixture-1",
                    "raw_name": "account_crm_lookup",
                    "tool_identity": "banking.customer.lookup",
                },
            ],
        },
        "redaction_policy": {
            "version": "fixture-1",
            "input_paths": [],
            "output_paths": ["$.intent"],
            "argument_paths": ["$.customer_id"],
            "result_paths": ["$.balance"],
        },
        "required_capabilities": ["tool_executions", "ordering"],
        "rule_scope_version": "attempt-root-parent-closure-v1",
        "limits": {},
    }
    return value | changes


def observed_banking_contract_payload():
    """与真实门禁交叉验证的本地银行示例配置；不作为生产默认来源资格。"""
    payload = contract_payload()
    payload["assembler_version"] = "local-banking-structure-1"
    entries = []
    for profile in payload["source_profile_set"]:
        pydantic = profile["profile_id"] == "pydantic-ai-v1"
        service = "pydantic_ai" if pydantic else "native_otel"
        profile["version"] = "local-banking-1"
        profile["fields"]["tool_raw_name"] = [["metadata", "attributes.gen_ai.tool.name"]]
        profile["structural_roles"] = [
            {
                "provider_type": "SPAN",
                "raw_name": service + ".business",
                "step_type": "AGENT",
                "safe_name": "business-scope",
                "metadata_equals": {
                    "scope.name": "synthetic-banking-business",
                    "scope.version": "1.0.0",
                    "resourceAttributes.service.name": service,
                    "resourceAttributes.service.version": "fixture-v1",
                },
            }
        ]
        if pydantic:
            framework_source = {
                "scope.name": "pydantic-ai",
                "scope.version": "2.55.0",
                "resourceAttributes.service.name": service,
                "resourceAttributes.service.version": "fixture-v1",
            }
            profile["structural_roles"].extend(
                [
                    {
                        "provider_type": kind,
                        "raw_name": raw,
                        "step_type": step_type,
                        "safe_name": safe,
                        "metadata_equals": framework_source,
                    }
                    for kind, raw, step_type, safe in (
                        ("AGENT", "agent run", "AGENT", "framework-agent"),
                        ("GENERATION", "chat function:plan:", "LLM", "framework-model"),
                        ("TOOL", "account_crm_lookup", "CUSTOM", "framework-tool-container"),
                        ("TOOL", "account_ledger_read", "CUSTOM", "framework-tool-container"),
                    )
                ]
            )
        for raw, logical in (
            ("crm_lookup", "banking.customer.lookup"),
            ("ledger_read", "banking.balance.read"),
            ("escalate", "banking.escalate"),
        ):
            raw = "account_" + raw if pydantic else raw
            entries.append(
                {
                    "source_profile_id": profile["profile_id"],
                    "source_profile_version": profile["version"],
                    "raw_name": raw,
                    "tool_identity": logical,
                }
            )
            profile["safe_names"].update({raw + suffix: raw + suffix for suffix in (".request", ".execution")})
    http_profile = profile_payload(
        "http-only-v1",
        version="local-banking-1",
        completion_protocol="root-only-v1",
        capabilities={key: "NONE" for key in payload["source_profile_set"][0]["capabilities"]},
    )
    http_profile["fields"]["duration_ms"] = [["metadata", "duration_ms"]]
    payload["source_profile_set"].append(http_profile)
    payload["tool_mapping"]["entries"] = entries
    return payload
