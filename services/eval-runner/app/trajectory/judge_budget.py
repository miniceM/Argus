"""短事务 Launch 行锁预算；发送标记提交后才能 await Provider。"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from ..db_models import ExperimentItemExecutionRecord, ExperimentLaunchRecord
from .codec import canonical_digest
from .judge import ParsedJudgeResult
from .judge_input import validate_prepared_judge_request
from .judge_provider import JudgeProviderOutcome, JudgeUsage
from .schema import Digest, Identity, _ContractModel

Count = Annotated[int, Field(ge=0, le=1000000000)]
Token = Annotated[str, Field(pattern=r"^[\x21-\x7e]{1,128}$")]
BindingId = Annotated[str, Field(pattern=r"^[\x21-\x7e]{1,256}$")]


class JudgeBudgetError(ValueError):
    """只携带受控错误码，不携带 JSON、凭据或原 Provider 回复。"""


class JudgeBindingBudget(_ContractModel):
    definition_digest: Digest
    max_input_tokens: Annotated[int, Field(ge=1, le=1000000000)]
    max_output_tokens: Annotated[int, Field(ge=1, le=16384)]


class FrozenJudgeBudget(_ContractModel):
    max_total_calls: Annotated[int, Field(ge=1, le=100000)]
    max_total_input_tokens: Annotated[int, Field(ge=1, le=1000000000)]
    max_total_output_tokens: Annotated[int, Field(ge=1, le=1000000000)]
    bindings: Annotated[dict[BindingId, JudgeBindingBudget], Field(min_length=1, max_length=1000)]

    @property
    def digest(self):
        return canonical_digest(
            {
                "schema_version": "judge-budget-v1",
                "profile": FrozenJudgeBudget.model_validate(self.model_dump()).model_dump(),
            }
        )


class JudgeBudgetCall(_ContractModel):
    launch_id: Identity
    item_id: Identity
    evaluation_generation: Count
    binding_id: BindingId
    input_digest: Digest
    definition_digest: Digest
    max_output_tokens: Annotated[int, Field(ge=1, le=16384)]

    @property
    def key(self):
        return canonical_digest(
            {
                "item_id": self.item_id,
                "evaluation_generation": self.evaluation_generation,
                "binding_id": self.binding_id,
            }
        )


class JudgeBudgetOwner(_ContractModel):
    kind: Literal["execution", "evaluation"]
    generation: Count
    lease_token: Token


class _Reservation(_ContractModel):
    call: JudgeBudgetCall
    owner: JudgeBudgetOwner
    token: Token
    state: Literal["RESERVED", "SENT", "SETTLED", "SETTLED_UNKNOWN", "CANCELLED"]
    charged_input_tokens: Count
    charged_output_tokens: Count


class _Ledger(_ContractModel):
    schema_version: Literal["1"] = "1"
    budget_digest: Digest
    reservations: dict[Digest, _Reservation]


@dataclass(frozen=True)
class JudgeBudgetReceipt:
    call: JudgeBudgetCall
    owner: JudgeBudgetOwner
    key: str
    token: str


def _error(code):
    raise JudgeBudgetError(code) from None


def _validated(model, value, code):
    try:
        return model.model_validate(value.model_dump() if type(value) is model else value)
    except (ValueError, TypeError, AttributeError):
        _error(code)


def _profile(launch):
    try:
        frozen = launch.manifest["judge_budget"]
        if launch.manifest["schema_version"] != "1.3" or set(frozen) != {"profile", "digest"}:
            raise ValueError
        profile = FrozenJudgeBudget.model_validate(frozen["profile"])
        if profile.digest != frozen["digest"]:
            raise ValueError
        return profile
    except (KeyError, ValueError, TypeError):
        _error("JUDGE_BUDGET_UNAVAILABLE")


def _usage_totals(entries):
    active = [value for value in entries.values() if value.state != "CANCELLED"]
    return len(active), sum(v.charged_input_tokens for v in active), sum(v.charged_output_tokens for v in active)


def _load(launch, profile):
    if launch.judge_budget_state_json is None:
        return _Ledger(budget_digest=profile.digest, reservations={})
    raw = launch.judge_budget_state_json
    if (
        type(raw) is not dict
        or type(raw.get("reservations")) is not dict
        or len(raw["reservations"]) > profile.max_total_calls
    ):
        _error("JUDGE_BUDGET_STATE_INVALID")
    ledger = _validated(_Ledger, launch.judge_budget_state_json, "JUDGE_BUDGET_STATE_INVALID")
    if ledger.budget_digest != profile.digest or len(ledger.reservations) > profile.max_total_calls:
        _error("JUDGE_BUDGET_STATE_INVALID")
    for key, row in ledger.reservations.items():
        bound = profile.bindings.get(row.call.binding_id)
        if (
            key != row.call.key
            or row.call.launch_id != launch.id
            or bound is None
            or row.call.definition_digest != bound.definition_digest
            or row.call.max_output_tokens != bound.max_output_tokens
            or row.charged_input_tokens > bound.max_input_tokens
            or row.charged_output_tokens > bound.max_output_tokens
        ):
            _error("JUDGE_BUDGET_STATE_INVALID")
        if row.state == "CANCELLED":
            if row.charged_input_tokens or row.charged_output_tokens:
                _error("JUDGE_BUDGET_STATE_INVALID")
        elif row.state in {"RESERVED", "SENT", "SETTLED_UNKNOWN"} and (
            row.charged_input_tokens != bound.max_input_tokens or row.charged_output_tokens != bound.max_output_tokens
        ):
            _error("JUDGE_BUDGET_STATE_INVALID")
    if any(
        a > b
        for a, b in zip(
            _usage_totals(ledger.reservations),
            (profile.max_total_calls, profile.max_total_input_tokens, profile.max_total_output_tokens),
            strict=True,
        )
    ):
        _error("JUDGE_BUDGET_STATE_INVALID")
    return ledger


def _launch(session, identity):
    if session.get_bind().dialect.name not in {"postgresql", "sqlite"}:
        _error("JUDGE_BUDGET_DIALECT_UNSUPPORTED")
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SET LOCAL lock_timeout = '2s'"))
        session.execute(text("SET LOCAL statement_timeout = '5s'"))
    value = session.scalar(
        select(ExperimentLaunchRecord)
        .where(ExperimentLaunchRecord.id == identity)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if value is None:
        _error("JUDGE_BUDGET_SCOPE_INVALID")
    return value


def _owner(session, launch, call, owner):
    # Launch → Item，与 Worker 全局锁顺序一致。网络期间不保留此事务。
    # 已取消的 Launch 不再等待 Item，避免与 Reconciler 的取消收尾反向等待。
    if launch.cancel_requested_at is not None or launch.status in {"CANCELLED", "CANCELLING"}:
        _error("JUDGE_BUDGET_CANCELLED")
    item = session.scalar(
        select(ExperimentItemExecutionRecord)
        .where(ExperimentItemExecutionRecord.id == call.item_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None or item.launch_id != launch.id or item.evaluation_generation != call.evaluation_generation:
        _error("JUDGE_BUDGET_SCOPE_INVALID")
    if owner.kind == "execution":
        if item.execution_status != "running":
            _error("JUDGE_BUDGET_LEASE_LOST")
        generation, token, expires = item.dispatch_generation, item.lease_token, item.lease_expires_at
    else:
        if item.evaluation_status != "evaluating":
            _error("JUDGE_BUDGET_LEASE_LOST")
        generation, token, expires = (
            item.evaluation_generation,
            item.evaluation_lease_token,
            item.evaluation_lease_expires_at,
        )
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)  # SQLite 测试的 DateTime 不保留 tz。
    if generation != owner.generation or token != owner.lease_token or expires is None or expires <= datetime.now(UTC):
        _error("JUDGE_BUDGET_LEASE_LOST")


def _save(launch, ledger, key, row):
    entries = ledger.reservations | {key: row}
    launch.judge_budget_state_json = ledger.model_copy(update={"reservations": entries}).model_dump(mode="json")


class JudgeBudgetStore:
    """自持短事务并提交；SQLite 只用于串行契约测试，分布式锁证明使用 PostgreSQL。"""

    def __init__(self, sessions):
        self.sessions = sessions

    def reserve(self, call, owner):
        call = _validated(JudgeBudgetCall, call, "JUDGE_BUDGET_SCOPE_INVALID")
        owner = _validated(JudgeBudgetOwner, owner, "JUDGE_BUDGET_LEASE_LOST")
        with self.sessions() as session:
            launch = _launch(session, call.launch_id)
            _owner(session, launch, call, owner)
            profile = _profile(launch)
            bound = profile.bindings.get(call.binding_id)
            if (
                bound is None
                or bound.definition_digest != call.definition_digest
                or bound.max_output_tokens != call.max_output_tokens
            ):
                _error("JUDGE_BUDGET_BINDING_MISMATCH")
            ledger = _load(launch, profile)
            previous = ledger.reservations.get(call.key)
            if previous is not None:
                if previous.call != call:
                    _error("JUDGE_CALL_IDENTITY_MISMATCH")
                if previous.state in {"SENT", "SETTLED_UNKNOWN", "SETTLED"}:
                    # 用量结算不是质量结果落库。恢复可复用已持久化结果，否则只能 UNKNOWN。
                    _error("JUDGE_CALL_OUTCOME_UNKNOWN")
                if previous.state == "RESERVED" and previous.owner == owner:
                    _error("JUDGE_CALL_ALREADY_RESERVED")
            elif len(ledger.reservations) >= profile.max_total_calls:
                # 仅淘汰从未发送的取消占位；已发送记录永久保留，旧 receipt 不能复活。
                retained = {key: row for key, row in ledger.reservations.items() if row.state != "CANCELLED"}
                ledger = ledger.model_copy(update={"reservations": retained})
                if len(retained) >= profile.max_total_calls:
                    _error("JUDGE_BUDGET_EXHAUSTED")
            entries = {k: v for k, v in ledger.reservations.items() if k != call.key}
            used_calls, used_input, used_output = _usage_totals(entries)
            if (
                used_calls + 1 > profile.max_total_calls
                or used_input + bound.max_input_tokens > profile.max_total_input_tokens
                or used_output + bound.max_output_tokens > profile.max_total_output_tokens
            ):
                _error("JUDGE_BUDGET_EXHAUSTED")
            token = uuid4().hex
            row = _Reservation(
                call=call,
                owner=owner,
                token=token,
                state="RESERVED",
                charged_input_tokens=bound.max_input_tokens,
                charged_output_tokens=bound.max_output_tokens,
            )
            _save(launch, ledger, call.key, row)
        return JudgeBudgetReceipt(call, owner, call.key, token)

    def _change(self, receipt, operation, usage=None):
        if type(receipt) is not JudgeBudgetReceipt:
            _error("JUDGE_RESERVATION_STALE")
        with self.sessions() as session:
            launch = _launch(session, receipt.call.launch_id)
            profile = _profile(launch)
            ledger = _load(launch, profile)
            row = ledger.reservations.get(receipt.key)
            if row is None or row.call != receipt.call or row.owner != receipt.owner or row.token != receipt.token:
                _error("JUDGE_RESERVATION_STALE")
            if operation == "send":
                _owner(session, launch, row.call, row.owner)
                if row.state != "RESERVED":
                    _error("JUDGE_CALL_OUTCOME_UNKNOWN")
                row = row.model_copy(update={"state": "SENT"})
            elif operation == "release":
                if row.state != "RESERVED":
                    return  # 已发送或结算的调用不能通过取消退款。
                row = row.model_copy(
                    update={"state": "CANCELLED", "charged_input_tokens": 0, "charged_output_tokens": 0}
                )
            else:
                if row.state not in {"SENT", "SETTLED_UNKNOWN", "SETTLED"}:
                    _error("JUDGE_CALL_NOT_SENT")
                bound = profile.bindings[row.call.binding_id]
                if usage is None:
                    if row.state == "SETTLED":
                        return
                    row = row.model_copy(update={"state": "SETTLED_UNKNOWN"})
                else:
                    if (
                        type(usage) is not JudgeUsage
                        or type(usage.input_tokens) is not int
                        or type(usage.output_tokens) is not int
                        or not 0 <= usage.input_tokens <= bound.max_input_tokens
                        or not 0 <= usage.output_tokens <= bound.max_output_tokens
                    ):
                        _error("JUDGE_USAGE_BOUND_EXCEEDED")
                    if row.state == "SETTLED" and (row.charged_input_tokens, row.charged_output_tokens) != (
                        usage.input_tokens,
                        usage.output_tokens,
                    ):
                        _error("JUDGE_USAGE_CONFLICT")
                    row = row.model_copy(
                        update={
                            "state": "SETTLED",
                            "charged_input_tokens": usage.input_tokens,
                            "charged_output_tokens": usage.output_tokens,
                        }
                    )
            _save(launch, ledger, receipt.key, row)

    def mark_sent(self, receipt):
        self._change(receipt, "send")

    def release_unsent(self, receipt):
        self._change(receipt, "release")

    def settle(self, receipt, *, usage):
        # 迟到用量仍属于已发生调用，允许旧 lease 结账，但绝不写质量结果。
        self._change(receipt, "settle", usage)


class BudgetedJudgeProvider:
    """连接实际受验证请求与 Provider；正式 Worker/冻结准入仍须显式接入。"""

    def __init__(self, provider, store):
        self.provider, self.store = provider, store

    async def evaluate(self, request, definition, *, binding_id, evaluation_generation, owner):
        receipt = None
        sent = False
        try:
            validate_prepared_judge_request(request, definition)
            call = JudgeBudgetCall(
                launch_id=request.scope.launch_id,
                item_id=request.scope.item_execution_id,
                evaluation_generation=evaluation_generation,
                binding_id=binding_id,
                input_digest=request.input_digest,
                definition_digest=request.definition_digest,
                max_output_tokens=definition.max_output_tokens,
            )
            pending = asyncio.create_task(asyncio.to_thread(self.store.reserve, call, owner))
            cancelled = False
            # thread 不能通过 task.cancel 撤销。包含重复 cancel：取回提交后的 receipt 才清理。
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    if cancelled:
                        raise asyncio.CancelledError from None
                    raise
            try:
                receipt = pending.result()
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
            if cancelled:
                raise asyncio.CancelledError
            await asyncio.to_thread(self.store.mark_sent, receipt)
            sent = True
            outcome = await self.provider.evaluate(request, definition)
            if (
                type(outcome) is not JudgeProviderOutcome
                or outcome.input_digest != request.input_digest
                or outcome.definition_digest != request.definition_digest
            ):
                _error("JUDGE_PROVIDER_RESULT_IDENTITY_MISMATCH")
            await asyncio.to_thread(self.store.settle, receipt, usage=outcome.usage)
            return outcome
        except asyncio.CancelledError:
            raise
        except JudgeBudgetError as error:
            code = str(error)
        except SQLAlchemyError:
            code = "JUDGE_BUDGET_STORAGE_UNAVAILABLE"
        except (ValueError, TypeError, AttributeError):
            code = "JUDGE_PREPARED_REQUEST_INVALID"
        except Exception:
            code = "JUDGE_PROVIDER_ERROR"
        finally:
            if receipt is not None and not sent:
                # mark_sent 的 thread 可能已提交。release 在同一行锁下只退 RESERVED。
                try:
                    await asyncio.shield(asyncio.to_thread(self.store.release_unsent, receipt))
                except Exception:
                    # 接管后的旧 receipt 或数据库失联不能吞掉取消；账本保守保留。
                    pass
        # 失败/失联保留 SENT 的最坏预留；不回退重发，也不伪造 usage。
        return JudgeProviderOutcome(ParsedJudgeResult("failed", None, code), None, None, None, 0)
