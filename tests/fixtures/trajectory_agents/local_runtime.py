"""Test-only local processes/API audit; no production reader or trajectory schema."""

from __future__ import annotations

import base64
import importlib.metadata
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

FIXTURES = Path(__file__).parent


def require_loopback(url):
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname or not ipaddress.ip_address(parsed.hostname).is_loopback:
        raise ValueError("S4 fixture requires an explicit HTTP loopback address")


def client_environment():
    # Docker 客户端配置仍由官方客户端读取，应用/Cloud 凭据不继承。
    return {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "TMPDIR")
            if key in os.environ}


def require_local_docker(env):
    context = env.get("DOCKER_CONTEXT")
    endpoint = env.get("DOCKER_HOST") if not context else None
    if not endpoint:
        command = ["docker", "context", "inspect"]
        if context:
            command.append(context)
        result = subprocess.run([*command, "--format", "{{json .Endpoints.docker.Host}}"],
                                env=env, capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise RuntimeError("S4 local Docker endpoint verification failed")
        endpoint = json.loads(result.stdout)
    parsed = urlparse(endpoint)
    if parsed.scheme != "unix" or parsed.netloc or not parsed.path.startswith("/"):
        raise ValueError("S4 fixture requires a local Unix Docker endpoint")


@contextmanager
def isolated_environment():
    # 仅本测试进程范围隔离 SDK、采样和代理设置；退出后恢复调用者配置。
    inherited = {key: value for key, value in os.environ.items()
                 if key.startswith(("LANGFUSE_", "OTEL_")) or
                 key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    try:
        for key in inherited:
            os.environ.pop(key, None)
        yield
    finally:
        os.environ.update(inherited)


class LocalLangfuse:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.project = "argus-s4-" + uuid.uuid4().hex[:12]
        self.env = client_environment()
        for key in ("DB_PASSWORD", "SALT", "ENCRYPTION_KEY", "CLICKHOUSE_PASSWORD", "MINIO_PASSWORD",
                    "AUTH_SECRET", "ADMIN_PASSWORD"):
            self.env["S4_" + key] = secrets.token_hex(32)
        self.env.update(S4_ORG_ID="s4-org-" + uuid.uuid4().hex,
                        S4_PROJECT_ID="s4-project-" + uuid.uuid4().hex,
                        S4_PUBLIC_KEY="pk-lf-" + uuid.uuid4().hex,
                        S4_SECRET_KEY="sk-lf-" + secrets.token_hex(32))
        self.command = ["docker", "compose", "--env-file", os.devnull, "-p", self.project,
                        "-f", str(FIXTURES / "compose.yaml")]
        self.reports = []
        self.agent_profiles = {}
        self.agent_packages = None
        self.sdk = None
        self.http = None
        self.export_session = None
        self._environment = None
        self._compose_started = False
        self.pagination = {}

    def compose(self, *args, timeout=660):
        result = subprocess.run([*self.command, *args], env=self.env, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            detail = result.stderr[-4000:]
            for key, value in self.env.items():
                if key.startswith("S4_"):
                    detail = detail.replace(value, "[fixture credential]")
            raise RuntimeError("S4 isolated Compose failed: " + detail)
        return result.stdout.strip()

    def __enter__(self):
        self._environment = isolated_environment()
        self._environment.__enter__()
        try:
            require_local_docker(self.env)
            self._compose_started = True
            self.compose("up", "--detach", "--pull", "never", "--wait", "--wait-timeout", "600")
            address = self.compose("port", "web", "3000")
            self.base_url = "http://" + address
            require_loopback(self.base_url)
            self.http = httpx.Client(base_url=self.base_url, timeout=20, trust_env=False,
                                     auth=(self.env["S4_PUBLIC_KEY"], self.env["S4_SECRET_KEY"]))
            # 读取实际部署契约；没有 API 就 fail closed，不使用旧接口掩盖版本差异。
            response = self.http.get("/api/openapi.yaml")
            response.raise_for_status()
            (self.directory / "langfuse-openapi.yaml").write_text(response.text)
            from langfuse import Langfuse
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace import TracerProvider
            from requests import Session
            self.export_session = Session()
            self.export_session.trust_env = False
            exporter = OTLPSpanExporter(endpoint=self.base_url + "/api/public/otel/v1/traces", timeout=20,
                headers={"Authorization": self.auth_header, "x-langfuse-ingestion-version": "4"},
                session=self.export_session)
            self.sdk = Langfuse(public_key=self.env["S4_PUBLIC_KEY"], secret_key=self.env["S4_SECRET_KEY"],
                                base_url=self.base_url, tracer_provider=TracerProvider(),
                                httpx_client=self.http, span_exporter=exporter, sample_rate=1.0,
                                debug=False, environment="s4-local", release="s4-local", timeout=20,
                                should_export_span=lambda span: True, flush_at=1, flush_interval=0.1)
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    @property
    def auth_header(self):
        credentials = self.env["S4_PUBLIC_KEY"] + ":" + self.env["S4_SECRET_KEY"]
        return "Basic " + base64.b64encode(credentials.encode()).decode()

    def observations(self, trace_id, limit=2):
        rows, cursors, page_sizes = [], [], []
        cursor = None
        for _ in range(100):
            params = {"traceId": trace_id, "limit": limit, "fields": "core,basic,time,io,metadata",
                      "expandMetadata": "attributes.workflow.event_ids,attributes.workflow.predecessor_event_ids"}
            if cursor:
                params["cursor"] = cursor
            response = self.http.get("/api/public/v2/observations", params=params)
            if response.status_code != 200:
                raise AssertionError(f"Local observation API returned HTTP {response.status_code}")
            payload = response.json()
            page = payload["data"]
            page_sizes.append(len(page))
            assert len(page) <= limit, "API ignored requested page size"
            assert all(row["traceId"] == trace_id for row in page), "API trace filter did not isolate this attempt"
            rows.extend(page)
            cursor = payload.get("meta", {}).get("cursor")
            if not cursor:
                break
            assert cursor not in cursors, "pagination cursor cycle"
            cursors.append(cursor)
        else:
            raise AssertionError("observation pagination limit exceeded")
        assert len({row["id"] for row in rows}) == len(rows), "duplicate observation across pages"
        self.pagination[(trace_id, limit)] = {"requested_page_size": limit, "pages": len(page_sizes),
                                             "page_sizes": page_sizes, "cursor_hops": len(cursors)}
        return rows

    def wait_observations(self, trace_id, minimum, timeout=90, *, require_inventory=True):
        deadline, started, polls = time.monotonic() + timeout, time.monotonic(), 0
        while time.monotonic() < deadline:
            rows = self.observations(trace_id)
            polls += 1
            terminal = next((row for row in rows if row.get("name") == "workflow.terminal"), None)
            inventory_complete = minimum <= 2
            if terminal:
                expected = set(terminal["metadata"]["attributes.workflow.event_ids"])
                actual = {row.get("metadata", {}).get("attributes.workflow.event_id") for row in rows}
                inventory_complete = expected <= actual
            observed_ids = {row["id"] for row in rows}
            parents_complete = all(row.get("parentObservationId") is None or
                                   row["parentObservationId"] in observed_ids for row in rows)
            if len(rows) >= minimum and parents_complete and all(row.get("endTime") for row in rows) and (not require_inventory or inventory_complete):
                self.last_visibility = {"elapsed_ms": round((time.monotonic() - started) * 1000), "polls": polls}
                return rows
            time.sleep(0.3)
        raise AssertionError(f"Local ingestion deadline: expected {minimum} observations; observed {len(rows)}")

    def record(self, profile, scenario, identity, rows, *, normalization=None):
        paged = self.observations(identity["trace_id"])
        full_page = self.observations(identity["trace_id"], limit=1000)
        if {row["id"] for row in full_page} != {row["id"] for row in paged}:
            # 新事件仍在进入时重新读取；比较的是同一采集窗口中的实际结果集合。
            rows = self.wait_observations(identity["trace_id"], len(full_page))
            paged = self.observations(identity["trace_id"])
            full_page = self.observations(identity["trace_id"], limit=1000)
        rows = paged
        assert {row["id"] for row in full_page} == {row["id"] for row in rows}, "paged and full-page sets differ"
        pagination = self.pagination[(identity["trace_id"], 2)]
        if len(rows) > 2:
            assert pagination["cursor_hops"] >= 1, "multi-page case did not exercise a continuation cursor"
        # 仅字段元数据与 ID 边，绝不持久化原始 input/output/metadata 值。
        matrix = {}
        for field in ("id", "traceId", "parentObservationId", "startTime", "endTime", "type", "name", "input", "output"):
            matrix[field] = {"total": len(rows), "absent": sum(field not in row for row in rows),
                             "null": sum(field in row and row[field] is None for row in rows),
                             "empty_string": sum(row.get(field) == "" for row in rows),
                             "empty_object": sum(isinstance(row.get(field), dict) and not row[field] for row in rows),
                             "empty_array": sum(isinstance(row.get(field), list) and not row[field] for row in rows),
                             "encoded_empty_object": sum(isinstance(row.get(field), str) and row[field].strip() == "{}" for row in rows),
                             "types": dict(Counter(type(row[field]).__name__ for row in rows if field in row))}
        physical_metadata = sorted({key for row in rows for key in (row.get("metadata") or {})})
        metadata_fields = {key: {"total": len(rows), "present": sum(key in (row.get("metadata") or {}) for row in rows),
            "types": dict(Counter(type(row["metadata"][key]).__name__ for row in rows if key in (row.get("metadata") or {})))}
            for key in physical_metadata}
        groups = {}
        for row in rows:
            phase = row.get("metadata", {}).get("attributes.workflow.phase", "INSTRUMENTATION")
            label = row["type"] + ":" + phase
            group = groups.setdefault(label, {"total": 0, "fields": {}})
            group["total"] += 1
            for field in matrix:
                counter = group["fields"].setdefault(field, {"present": 0, "null": 0, "types": {}})
                if field in row:
                    counter["present"] += 1
                    counter["null"] += row[field] is None
                    kind = type(row[field]).__name__
                    counter["types"][kind] = counter["types"].get(kind, 0) + 1
        event_rows = [row for row in rows if row.get("metadata", {}).get("attributes.workflow.event_id")]
        by_event = {row["metadata"]["attributes.workflow.event_id"]: row for row in event_rows}
        assert len(by_event) == len(event_rows), "duplicate business event ID"
        dependency_edges = []
        for row in event_rows:
            for predecessor_id in row["metadata"].get("attributes.workflow.predecessor_event_ids", []):
                predecessor = by_event[predecessor_id]
                assert predecessor["metadata"]["attributes.workflow.scope_id"] == row["metadata"]["attributes.workflow.scope_id"]
                assert predecessor["metadata"]["attributes.workflow.status"] == "SUCCEEDED"
                dependency_edges.append({"event_id": row["metadata"]["attributes.workflow.event_id"],
                    "observation_id": row["id"], "predecessor_event_id": predecessor_id,
                    "predecessor_observation_id": predecessor["id"],
                    "phase": row["metadata"]["attributes.workflow.phase"],
                    "predecessor_phase": predecessor["metadata"]["attributes.workflow.phase"]})
        sequences = [row["metadata"]["attributes.workflow.sequence"] for row in event_rows]
        assert len(set(sequences)) == len(sequences), "duplicate sequence in the captured workflow"
        assert all(datetime.fromisoformat(row["startTime"].replace("Z", "+00:00")).tzinfo for row in rows)
        assert all(datetime.fromisoformat(row["endTime"].replace("Z", "+00:00")) >=
                   datetime.fromisoformat(row["startTime"].replace("Z", "+00:00")) for row in rows)
        self.reports.append({"profile": profile, "scenario": scenario, "identity": identity,
                             "visibility": self.last_visibility, "fields": matrix,
                             "pagination": {**pagination, "unique_count": len(rows), "full_page_equal": True},
                             "span_classes": groups, "metadata_fields": metadata_fields,
                             "causal_sources": {"parent_edges": True,
                                 "otel_links_exposed": any("links" in row for row in rows),
                                 "business_predecessors": any("attributes.workflow.predecessor_event_ids" in
                                     row.get("metadata", {}) for row in rows),
                                 "verified_business_dependency_edges": len(dependency_edges),
                                 "sequence_unique": len(set(sequences)) == len(sequences),
                                 "human_events": sum(row.get("metadata", {}).get("attributes.workflow.phase") == "HUMAN" for row in rows)},
                             "dependency_edges": dependency_edges,
                             "edges": [{"id": row["id"], "parent": row.get("parentObservationId")} for row in rows]})
        if normalization is not None:
            self.reports[-1]["normalization"] = normalization
        report = {"captured_at": datetime.now(UTC).isoformat(), "server": "4.38.0",
                  "runner_sdk": importlib.metadata.version("langfuse"), "agent_packages": self.agent_packages,
                  "api": "/api/public/v2/observations", "poll_interval_ms": 300, "cases": self.reports}
        (self.directory / "field-matrix.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    def __exit__(self, exc_type, exc_value, traceback):
        errors = []
        operations = [resource for resource in (
            self.sdk.shutdown if self.sdk else None, self.http.close if self.http else None,
            self.export_session.close if self.export_session else None,
            (lambda: self.compose("down", "--volumes", "--remove-orphans", timeout=120))
                if self._compose_started else None) if resource]
        try:
            for operation in operations:
                try:
                    operation()
                except Exception as error:
                    errors.append(error)
        finally:
            if self._environment:
                self._environment.__exit__(exc_type, exc_value, traceback)
        if errors:
            original = exc_value if exc_value is not None else errors[0]
            original.add_note("S4 fixture cleanup failures: " + ", ".join(type(error).__name__ for error in errors))
            if exc_value is None:
                raise original


class AgentProcess:
    def __init__(self, runtime, directory, profile, *, export_mode="immediate", approval=False):
        self.runtime, self.directory, self.profile = runtime, Path(directory), profile
        self.export_mode, self.approval = export_mode, approval
        self.approval_process = self.approval_log = None
        self.process = self.log = None

    def __enter__(self):
        python = FIXTURES / ".venv" / "bin" / "python"
        if not python.is_file():
            raise RuntimeError("Prepare the isolated Agent environment from the hash-locked requirements first")
        if self.runtime.agent_packages is None:
            closure = subprocess.check_output([str(python), "-c", "import importlib.metadata,json; "
                "print(json.dumps({d.metadata['Name'].lower().replace('_','-'):d.version "
                "for d in importlib.metadata.distributions()}))"], text=True)
            packages = json.loads(closure)
            forbidden = {"langfuse", "langsmith", "pydantic-evals", "pydantic-ai", "logfire", "argus-eval", "argus-eval-sdk"}
            if forbidden.intersection(packages):
                raise AssertionError("An Evaluation SDK is installed in the business Agent environment")
            self.runtime.agent_packages = packages
        self.ready = self.directory / (self.profile + "-ready.json")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1",
               "S4_READY_FILE": str(self.ready), "S4_PROFILE": self.profile,
               "S4_EXPORT_MODE": self.export_mode,
               "S4_OTLP_URL": self.runtime.base_url + "/api/public/otel/v1/traces",
               "S4_OTLP_AUTH": self.runtime.auth_header}
        try:
            if self.approval:
                approval_ready = self.directory / "approval-ready.json"
                self.approval_log = (self.directory / "approval.log").open("w")
                self.approval_process = subprocess.Popen([str(python), str(FIXTURES / "approval_system.py")],
                    env={**env, "S4_READY_FILE": str(approval_ready)}, stdout=self.approval_log, stderr=self.approval_log)
                deadline = time.monotonic() + 20
                while not approval_ready.exists():
                    if self.approval_process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("S4 approval system readiness failed")
                    time.sleep(0.05)
                env["S4_APPROVAL_URL"] = json.loads(approval_ready.read_text())["endpoint"]
                require_loopback(env["S4_APPROVAL_URL"])
            self.log = (self.directory / (self.profile + ".log")).open("w")
            self.process = subprocess.Popen([str(python), str(FIXTURES / self.profile / "agent.py")],
                                            env=env, stdout=self.log, stderr=self.log)
            deadline = time.monotonic() + 20
            while not self.ready.exists():
                if self.process.poll() is not None:
                    raise RuntimeError(f"S4 {self.profile} process failed; inspect its redacted fixture log")
                if time.monotonic() >= deadline:
                    raise RuntimeError("S4 Agent readiness deadline")
                time.sleep(0.05)
            self.endpoint = json.loads(self.ready.read_text())["endpoint"]
            require_loopback(self.endpoint)
            self.runtime.agent_profiles[self.endpoint] = self.profile
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        errors = []
        for process, log in ((self.process, self.log), (self.approval_process, self.approval_log)):
            try:
                if process:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            except Exception as error:
                errors.append(error)
            finally:
                if log:
                    log.close()
        if errors:
            original = exc_value if exc_value is not None else errors[0]
            original.add_note("S4 process cleanup failures: " + ", ".join(type(error).__name__ for error in errors))
            if exc_value is None:
                raise original


def assert_lineage(rows, identity):
    by_id = {row["id"]: row for row in rows}
    assert identity["root_id"] in by_id
    assert by_id[identity["root_id"]]["name"] == "remote-agent-http"
    assert all(row["traceId"] == identity["trace_id"] for row in rows)
    for row in rows:
        seen = set()
        current = row
        while current.get("parentObservationId"):
            parent = current["parentObservationId"]
            assert parent not in seen, "parent cycle"
            seen.add(parent)
            if parent not in by_id:
                raise AssertionError("orphan observation in this attempt scope")
            current = by_id[parent]
        assert current["name"] == "eval_item_execution:synthetic-case"
