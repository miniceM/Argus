#!/usr/bin/env python3
"""Verify that a deployed Langfuse service matches an immutable GHCR image."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

IMAGE_RE = re.compile(
    r"^ghcr\.io/(?P<repository>[a-z0-9._/-]+)@(?P<digest>sha256:[0-9a-f]{64})$"
)


def _request_json(url: str, headers: dict[str, str]) -> dict:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def _registry_token(repository: str) -> str:
    query = urlencode(
        {
            "service": "ghcr.io",
            "scope": f"repository:{repository}:pull",
        }
    )
    headers: dict[str, str] = {}
    username = os.environ.get("LANGFUSE_GHCR_USERNAME")
    token = os.environ.get("LANGFUSE_GHCR_TOKEN")
    if username and token:
        credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
    payload = _request_json(f"https://ghcr.io/token?{query}", headers)
    return payload["token"]


def _manifest_config_digest(repository: str, digest: str, token: str) -> str:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": ", ".join(
            (
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            )
        ),
    }
    manifest = _request_json(
        f"https://ghcr.io/v2/{repository}/manifests/{digest}", headers
    )
    manifests = manifest.get("manifests")
    if manifests:
        amd64 = next(
            (
                item
                for item in manifests
                if item.get("platform", {}).get("architecture") == "amd64"
                and item.get("platform", {}).get("os") == "linux"
            ),
            None,
        )
        if amd64 is None:
            raise RuntimeError("GHCR image has no linux/amd64 manifest")
        manifest = _request_json(
            f"https://ghcr.io/v2/{repository}/manifests/{amd64['digest']}",
            headers,
        )
    config_digest = manifest.get("config", {}).get("digest")
    if not config_digest:
        raise RuntimeError("GHCR image manifest has no config digest")
    return config_digest


def _build_id_from_registry(repository: str, config_digest: str, token: str) -> str:
    payload = _request_json(
        f"https://ghcr.io/v2/{repository}/blobs/{config_digest}",
        {"Authorization": f"Bearer {token}"},
    )
    for value in payload.get("config", {}).get("Env", []):
        if value.startswith("BUILD_ID="):
            return value.removeprefix("BUILD_ID=")
    raise RuntimeError("GHCR image config has no BUILD_ID")


def _deployed_build_id(base_url: str) -> str:
    payload = _request_json(
        f"{base_url.rstrip('/')}/api/public/argus-image-identity", {}
    )
    build_id = payload.get("buildId")
    if not isinstance(build_id, str) or not build_id:
        raise RuntimeError("deployed Langfuse identity has no buildId")
    return build_id


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: verify-image-identity.py IMAGE_DIGEST BASE_URL EXPECTED_BUILD_ID")
        return 2

    image_ref, base_url, expected_build_id = sys.argv[1:]
    match = IMAGE_RE.fullmatch(image_ref)
    if match is None:
        raise RuntimeError("IMAGE_DIGEST must be a full ghcr.io digest reference")

    repository = match.group("repository")
    digest = match.group("digest")
    token = _registry_token(repository)
    config_digest = _manifest_config_digest(repository, digest, token)
    registry_build_id = _build_id_from_registry(repository, config_digest, token)
    deployed_build_id = _deployed_build_id(base_url)

    if registry_build_id != expected_build_id:
        raise RuntimeError(
            f"release build identity mismatch: registry={registry_build_id}, "
            f"expected={expected_build_id}"
        )
    if deployed_build_id != registry_build_id:
        raise RuntimeError(
            f"deployed build identity mismatch: registry={registry_build_id}, "
            f"deployed={deployed_build_id}"
        )

    print(
        json.dumps(
            {
                "image": image_ref,
                "registryBuildId": registry_build_id,
                "deployedBuildId": deployed_build_id,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HTTPError, KeyError, RuntimeError, ValueError) as error:
        print(f"image identity verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
