"""Small GitHub REST client used by trusted default-branch CI controllers."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class GitHubApiError(RuntimeError):
    """A GitHub API call failed or returned an unusable response."""


class _StripCrossHostAuthorization(HTTPRedirectHandler):
    def redirect_request(self, request: Request, response: Any, code: int, message: str, headers: Any, new_url: str):
        redirected = super().redirect_request(request, response, code, message, headers, new_url)
        if redirected is None:
            return None
        if urlsplit(request.full_url).netloc.lower() != urlsplit(new_url).netloc.lower():
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected


class GitHubClient:
    """Authenticated, bounded GitHub REST access with explicit pagination."""

    def __init__(
        self,
        *,
        api_url: str,
        repository: str,
        token: str,
        timeout: float = 20.0,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
            raise ValueError("repository must be owner/name")
        if not token:
            raise ValueError("GitHub API token is required")
        self.api_url = api_url.rstrip("/") + "/"
        self.repository = repository
        self.token = token
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._opener = build_opener(_StripCrossHostAuthorization())

    @property
    def repo_path(self) -> str:
        owner, repo = self.repository.split("/", 1)
        return f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

    def _open(self, request: Request) -> Any:
        return self._opener.open(request, timeout=self.timeout)

    def _request_bytes(self, path: str, *, params: Mapping[str, str] | None = None) -> tuple[bytes, Mapping[str, str]]:
        relative = path.lstrip("/")
        url = urljoin(self.api_url, relative)
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Argus-Cloud-E2E-Policy",
            },
        )
        try:
            with self._open(request) as response:
                payload = response.read(self.max_response_bytes + 1)
                if len(payload) > self.max_response_bytes:
                    raise GitHubApiError("GitHub API response exceeds configured size limit")
                return payload, dict(response.headers.items())
        except HTTPError as exc:
            raise GitHubApiError(f"GitHub API returned HTTP {exc.code} for {path}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise GitHubApiError(f"GitHub API request failed for {path}: {type(exc).__name__}") from None

    def request_json(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        if method != "GET" or body is not None:
            url = urljoin(self.api_url, path.lstrip("/"))
            if params:
                url += ("&" if "?" in url else "?") + urlencode(params)
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            request = Request(
                url,
                data=payload,
                method=method,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "Argus-Cloud-E2E-Policy",
                },
            )
            try:
                with self._open(request) as response:
                    raw = response.read(self.max_response_bytes + 1)
                    if len(raw) > self.max_response_bytes:
                        raise GitHubApiError("GitHub API response exceeds configured size limit")
            except HTTPError as exc:
                raise GitHubApiError(f"GitHub API returned HTTP {exc.code} for {path}") from None
            except (URLError, TimeoutError, OSError) as exc:
                raise GitHubApiError(f"GitHub API request failed for {path}: {type(exc).__name__}") from None
        else:
            raw, _ = self._request_bytes(path, params=params)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubApiError(f"GitHub API returned invalid JSON for {path}") from exc

    def paginate(
        self,
        path: str,
        *,
        array_key: str | None = None,
        params: Mapping[str, str] | None = None,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> list[Any]:
        result: list[Any] = []
        for page in range(1, max_pages + 1):
            page_params = dict(params or {})
            page_params.update({"per_page": str(page_size), "page": str(page)})
            response = self.request_json(path, params=page_params)
            if array_key is None:
                items = response
            else:
                items = response.get(array_key) if isinstance(response, dict) else None
            if not isinstance(items, list):
                raise GitHubApiError(f"GitHub API pagination response has no {array_key or 'array'}")
            result.extend(items)
            if len(items) < page_size:
                return result
        raise GitHubApiError(f"GitHub API pagination exceeded {max_pages} pages for {path}")

    def get_run(self, run_id: int) -> dict[str, Any]:
        response = self.request_json(f"{self.repo_path}/actions/runs/{run_id}")
        if not isinstance(response, dict):
            raise GitHubApiError("workflow run response is not an object")
        return response

    def list_jobs(self, run_id: int, attempt: int) -> list[dict[str, Any]]:
        jobs = self.paginate(
            f"{self.repo_path}/actions/runs/{run_id}/attempts/{attempt}/jobs",
            array_key="jobs",
        )
        if not all(isinstance(job, dict) for job in jobs):
            raise GitHubApiError("workflow jobs response contains malformed entries")
        return jobs

    def get_ref(self, ref: str) -> dict[str, Any]:
        response = self.request_json(f"{self.repo_path}/git/ref/{quote(ref, safe='/')}")
        if not isinstance(response, dict):
            raise GitHubApiError("Git reference response is not an object")
        return response

    def get_pull_request(self, number: int) -> dict[str, Any]:
        response = self.request_json(f"{self.repo_path}/pulls/{number}")
        if not isinstance(response, dict):
            raise GitHubApiError("pull request response is not an object")
        return response

    def get_workflow_id(self, workflow_path: str) -> int:
        workflows = self.paginate(f"{self.repo_path}/actions/workflows", array_key="workflows")
        matches = [workflow for workflow in workflows if isinstance(workflow, dict) and workflow.get("path") == workflow_path]
        if (
            len(matches) != 1
            or isinstance(matches[0].get("id"), bool)
            or not isinstance(matches[0].get("id"), int)
            or matches[0]["id"] <= 0
        ):
            raise GitHubApiError(f"expected exactly one registered workflow at {workflow_path}")
        if matches[0].get("state") != "active":
            raise GitHubApiError(f"workflow is not active: {workflow_path}")
        return matches[0]["id"]

    def list_workflow_runs(self, workflow_id: int | str, *, params: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        if (
            isinstance(workflow_id, bool)
            or not str(workflow_id).strip()
            or (isinstance(workflow_id, int) and workflow_id <= 0)
        ):
            raise ValueError("workflow identifier is required and numeric IDs must be positive")
        encoded = quote(str(workflow_id), safe="")
        runs = self.paginate(
            f"{self.repo_path}/actions/workflows/{encoded}/runs",
            array_key="workflow_runs",
            params=params,
        )
        if not all(isinstance(run, dict) for run in runs):
            raise GitHubApiError("workflow runs response contains malformed entries")
        return runs

    def list_open_pull_requests(self, *, base: str = "main") -> list[dict[str, Any]]:
        """Return every open PR targeting ``base`` using bounded API pagination."""
        if not isinstance(base, str) or not base.strip() or any(char in base for char in "\r\n"):
            raise ValueError("pull request base branch is required")
        pulls = self.paginate(
            f"{self.repo_path}/pulls",
            params={"state": "open", "base": base, "sort": "updated", "direction": "desc"},
        )
        if not all(isinstance(item, dict) for item in pulls):
            raise GitHubApiError("open pull requests response contains malformed entries")
        result: list[dict[str, Any]] = []
        for pull in pulls:
            pull_base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
            number = pull.get("number")
            if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                raise GitHubApiError("open pull request has an invalid number")
            if pull.get("state") == "open" and pull_base.get("ref") == base:
                result.append(pull)
        return result

    def list_pull_request_files(self, number: int) -> list[dict[str, Any]]:
        files = self.paginate(f"{self.repo_path}/pulls/{number}/files")
        if len(files) >= 3000:
            raise GitHubApiError("pull request file list reached GitHub's 3000-file limit")
        if not all(isinstance(item, dict) for item in files):
            raise GitHubApiError("pull request files response contains malformed entries")
        return files

    def get_file_text(self, path: str, ref: str, *, max_bytes: int = 1_000_000) -> str:
        encoded_path = "/".join(quote(segment, safe="") for segment in path.split("/"))
        response = self.request_json(
            f"{self.repo_path}/contents/{encoded_path}", params={"ref": ref}
        )
        if not isinstance(response, dict) or response.get("type") != "file":
            raise GitHubApiError(f"repository path is not a regular file: {path}")
        if response.get("encoding") != "base64" or not isinstance(response.get("content"), str):
            raise GitHubApiError(f"repository content encoding is unsupported: {path}")
        try:
            raw = base64.b64decode(response["content"], validate=False)
            if len(raw) > max_bytes:
                raise GitHubApiError(f"repository file exceeds size limit: {path}")
            return raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubApiError(f"repository file is not valid UTF-8: {path}") from exc

    def get_tree_modes(self, commit_sha: str) -> tuple[dict[str, str], bool]:
        response = self.request_json(
            f"{self.repo_path}/git/trees/{commit_sha}", params={"recursive": "1"}
        )
        if not isinstance(response, dict) or not isinstance(response.get("tree"), list):
            raise GitHubApiError("Git tree response is malformed")
        modes = {
            str(entry["path"]): str(entry["mode"])
            for entry in response["tree"]
            if isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("mode"), str)
        }
        return modes, not bool(response.get("truncated", False))

    def list_artifacts(self, run_id: int) -> list[dict[str, Any]]:
        artifacts = self.paginate(
            f"{self.repo_path}/actions/runs/{run_id}/artifacts", array_key="artifacts"
        )
        if not all(isinstance(item, dict) for item in artifacts):
            raise GitHubApiError("workflow artifacts response contains malformed entries")
        return artifacts

    def download_artifact(self, artifact_id: int, *, max_bytes: int = 2_000_000) -> bytes:
        # The API redirects to a short-lived object-store URL. The opener strips
        # Authorization and Cookie headers if that redirect crosses hosts.
        original_limit = self.max_response_bytes
        try:
            self.max_response_bytes = max_bytes
            payload, _ = self._request_bytes(f"{self.repo_path}/actions/artifacts/{artifact_id}/zip")
        finally:
            self.max_response_bytes = original_limit
        return payload

    def create_commit_status(self, sha: str, *, state: str, context: str, description: str, target_url: str | None = None) -> Any:
        body: dict[str, Any] = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        return self.request_json(
            f"{self.repo_path}/statuses/{sha}", method="POST", body=body
        )

    def list_run_approvals(self, run_id: int) -> list[dict[str, Any]]:
        approvals = self.paginate(f"{self.repo_path}/actions/runs/{run_id}/approvals")
        if not all(isinstance(item, dict) for item in approvals):
            raise GitHubApiError("workflow approvals response contains malformed entries")
        return approvals
