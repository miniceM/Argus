#!/usr/bin/env python3
"""Seed realistic demo data for Argus Console preview."""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db import DatabaseManager, MigrationRunner  # noqa: E402
from app.db_models import (  # noqa: E402
    AgentRecord,
    AgentVersionRecord,
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)


def seed():
    db_manager = DatabaseManager.from_env()
    runner = MigrationRunner(db_manager.engine, ROOT / "migrations")
    runner.apply_all()

    now = datetime.now(UTC)

    with db_manager.get_session() as session:
        # 1. Upsert Agent & Versions
        agent = session.query(AgentRecord).filter_by(id="banking-agent").first()
        if not agent:
            agent = AgentRecord(
                id="banking-agent",
                name="智能银行客服 Agent",
                description="面向零售银行零售客户的智能客服助手，负责解答账户、转账、信用卡等常见咨询业务。",
            )
            session.add(agent)
            session.flush()

        v1 = session.query(AgentVersionRecord).filter_by(agent_id="banking-agent", version="v1").first()
        if not v1:
            v1 = AgentVersionRecord(
                id="banking-agent-v1",
                agent_id="banking-agent",
                version="v1",
                endpoint="http://127.0.0.1:18081/invoke",
                method="POST",
                timeout_seconds=5.0,
                max_retries=2,
                rate_limit_per_minute=600,
                max_concurrency=4,
                request_mapping={"messages": "input.messages", "customer_id": "input.customer_id"},
                is_idempotent=False,
                spec_digest="sha256:banking-v1-spec-digest",
            )
            session.add(v1)

        v2 = session.query(AgentVersionRecord).filter_by(agent_id="banking-agent", version="v2").first()
        if not v2:
            v2 = AgentVersionRecord(
                id="banking-agent-v2",
                agent_id="banking-agent",
                version="v2",
                endpoint="http://127.0.0.1:18082/invoke",
                method="POST",
                timeout_seconds=5.0,
                max_retries=2,
                rate_limit_per_minute=600,
                max_concurrency=8,
                request_mapping={"messages": "input.messages", "customer_id": "input.customer_id"},
                is_idempotent=True,
                spec_digest="sha256:banking-v2-spec-digest",
            )
            session.add(v2)
        session.flush()

        # Helper to clean up existing demo launches
        demo_ids = [
            "launch-preview-pending",
            "launch-preview-partial-failed",
            "launch-preview-completed",
        ]
        for lid in demo_ids:
            session.query(ExecutionAttemptRecord).filter(
                ExecutionAttemptRecord.item_execution_id.in_(
                    session.query(ExperimentItemExecutionRecord.id).filter_by(launch_id=lid)
                )
            ).delete(synchronize_session=False)
            session.query(ExperimentItemExecutionRecord).filter_by(launch_id=lid).delete()
            session.query(ExperimentLaunchRecord).filter_by(id=lid).delete()
        session.flush()

        # 2. Launch 1: PENDING (Ready to Run)
        l_pending = ExperimentLaunchRecord(
            id="launch-preview-pending",
            name="银行客服 v1 基础合规性冒烟测试 (准备就绪)",
            status="PENDING",
            quality_conclusion="unknown",
            dataset_id="ds-banking-core",
            dataset_name="banking-smoke-dataset",
            dataset_version="v2.1",
            agent_id="banking-agent",
            agent_version="v1",
            agent_version_id=v1.id,
            manifest={"schema_version": "1.0", "items_count": 4},
            created_by="ReleaseEngineer",
            created_at=now - timedelta(minutes=10),
            updated_at=now - timedelta(minutes=10),
        )
        session.add(l_pending)
        session.flush()

        for idx, (item_id, _prompt) in enumerate(
            [
                ("item-101", "查询我的储蓄卡当前可用余额是多少"),
                ("item-102", "如何在手机银行 App 上开通境外支付限额"),
                ("item-103", "我的信用卡丢失了，请立刻帮我挂失并补卡"),
                ("item-104", "跨行转账手续费是多少，多久能到账？"),
            ],
            1,
        ):
            session.add(
                ExperimentItemExecutionRecord(
                    id=f"item-exec-p-{idx}",
                    launch_id=l_pending.id,
                    dataset_item_id=item_id,
                    execution_status="pending",
                    eval_status="pending",
                    quality_conclusion="unknown",
                    dispatch_generation=1,
                    created_at=now - timedelta(minutes=10),
                    updated_at=now - timedelta(minutes=10),
                )
            )

        # 3. Launch 2: PARTIAL_FAILED with Ambiguous Outcome & Retry Wait
        l_partial = ExperimentLaunchRecord(
            id="launch-preview-partial-failed",
            name="银行客服 v1 高并发压力回归评测 (存在熔断与超时)",
            status="PARTIAL_FAILED",
            quality_conclusion="failed",
            dataset_id="ds-banking-core",
            dataset_name="banking-comprehensive-dataset",
            dataset_version="v2.1",
            agent_id="banking-agent",
            agent_version="v1",
            agent_version_id=v1.id,
            manifest={"schema_version": "1.0", "items_count": 5},
            created_by="QA_Lead",
            started_at=now - timedelta(minutes=25),
            completed_at=now - timedelta(minutes=2),
            created_at=now - timedelta(minutes=30),
            updated_at=now - timedelta(minutes=2),
            status_reason="Execution completed with 2 errors (1 Ambiguous outcome crash)",
        )
        session.add(l_partial)
        session.flush()

        # Items for Launch 2
        items_data = [
            ("item-201", "COMPLETED", "completed", "passed", 0, None, None, None),
            ("item-202", "COMPLETED", "completed", "passed", 0, None, None, None),
            ("item-203", "COMPLETED", "completed", "passed", 0, None, None, None),
            ("item-204", "FAILED", "failed", "failed", 2, "Agent returned 504 Gateway Timeout", None, None),
            (
                "item-205",
                "FAILED",
                "failed",
                "failed",
                1,
                "AMBIGUOUS_OUTCOME: Worker crashed while request in flight (non-idempotent)",
                None,
                "AMBIGUOUS_OUTCOME",
            ),
        ]

        for idx, (
            item_id,
            item_status,
            eval_status,
            q_conc,
            _retry_cnt,
            err_msg,
            _lease_owner,
            _phase_tag,
        ) in enumerate(items_data, 1):
            item_exec = ExperimentItemExecutionRecord(
                id=f"item-exec-pf-{idx}",
                launch_id=l_partial.id,
                dataset_item_id=item_id,
                execution_status=item_status.lower(),
                eval_status=eval_status,
                quality_conclusion=q_conc,
                execution_error=err_msg,
                dispatch_generation=1,
                created_at=now - timedelta(minutes=25),
                updated_at=now - timedelta(minutes=2),
                started_at=now - timedelta(minutes=24),
                completed_at=now - timedelta(minutes=2) if item_status in ("COMPLETED", "FAILED") else None,
            )
            session.add(item_exec)
            session.flush()

            # Add Attempts
            if idx == 5:
                # Ambiguous outcome attempt
                attempt = ExecutionAttemptRecord(
                    id=f"attempt-pf-{idx}-1",
                    item_execution_id=item_exec.id,
                    attempt_no=1,
                    status="FAILED",
                    error_message="Worker connection dropped while waiting for downstream agent response",
                    worker_id="worker-node-alpha-02",
                    request_phase="MAY_HAVE_BEEN_SENT",
                    started_at=now - timedelta(minutes=20),
                    completed_at=now - timedelta(minutes=19),
                )
                session.add(attempt)
                item_exec.final_attempt_id = attempt.id
            elif idx == 4:
                # 2 retries
                att1 = ExecutionAttemptRecord(
                    id=f"attempt-pf-{idx}-1",
                    item_execution_id=item_exec.id,
                    attempt_no=1,
                    status="FAILED",
                    error_message="HTTP 504 Gateway Timeout from backend endpoint",
                    worker_id="worker-node-alpha-01",
                    request_phase="RESPONSE_RECEIVED",
                    started_at=now - timedelta(minutes=22),
                    completed_at=now - timedelta(minutes=21),
                )
                session.add(att1)
                att2 = ExecutionAttemptRecord(
                    id=f"attempt-pf-{idx}-2",
                    item_execution_id=item_exec.id,
                    attempt_no=2,
                    status="FAILED",
                    error_message="HTTP 504 Gateway Timeout from backend endpoint (retry exhausted)",
                    worker_id="worker-node-alpha-03",
                    request_phase="RESPONSE_RECEIVED",
                    started_at=now - timedelta(minutes=18),
                    completed_at=now - timedelta(minutes=17),
                )
                session.add(att2)
                item_exec.final_attempt_id = att2.id
            else:
                # Success
                att = ExecutionAttemptRecord(
                    id=f"attempt-pf-{idx}-1",
                    item_execution_id=item_exec.id,
                    attempt_no=1,
                    status="COMPLETED",
                    worker_id="worker-node-alpha-01",
                    request_phase="RESPONSE_RECEIVED",
                    started_at=now - timedelta(minutes=23),
                    completed_at=now - timedelta(minutes=22),
                )
                session.add(att)
                item_exec.final_attempt_id = att.id

        # 4. Launch 3: COMPLETED (Pass All)
        l_comp = ExperimentLaunchRecord(
            id="launch-preview-completed",
            name="银行客服 v2 生产准入全量门禁评测 (已通过)",
            status="COMPLETED",
            quality_conclusion="passed",
            dataset_id="ds-banking-core",
            dataset_name="banking-comprehensive-dataset",
            dataset_version="v2.2",
            agent_id="banking-agent",
            agent_version="v2",
            agent_version_id=v2.id,
            manifest={"schema_version": "1.0", "items_count": 6},
            created_by="CI_Pipeline",
            started_at=now - timedelta(hours=2),
            completed_at=now - timedelta(hours=1, minutes=45),
            created_at=now - timedelta(hours=2, minutes=5),
            updated_at=now - timedelta(hours=1, minutes=45),
            langfuse_experiment_url="https://cloud.langfuse.com/project/demo/datasets/banking-comprehensive-dataset/experiments/exp-bank-v2-gate",
            status_reason="All 6 items passed deterministic and trajectory evaluation gates",
        )
        session.add(l_comp)
        session.flush()

        for idx in range(1, 7):
            it = ExperimentItemExecutionRecord(
                id=f"item-exec-c-{idx}",
                launch_id=l_comp.id,
                dataset_item_id=f"item-30{idx}",
                execution_status="completed",
                eval_status="completed",
                quality_conclusion="passed",
                dispatch_generation=1,
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=1, minutes=45),
                started_at=now - timedelta(hours=2),
                completed_at=now - timedelta(hours=1, minutes=45),
            )
            session.add(it)
            session.flush()
            att = ExecutionAttemptRecord(
                id=f"attempt-c-{idx}-1",
                item_execution_id=it.id,
                attempt_no=1,
                status="COMPLETED",
                worker_id="worker-node-prod-01",
                request_phase="RESPONSE_RECEIVED",
                started_at=now - timedelta(hours=2),
                completed_at=now - timedelta(hours=1, minutes=45),
            )
            session.add(att)
            it.final_attempt_id = att.id

        session.commit()
        print("Successfully seeded preview data for Argus Console!")


if __name__ == "__main__":
    seed()
