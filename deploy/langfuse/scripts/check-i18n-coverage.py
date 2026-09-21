#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_RE = re.compile(r"\{\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:[,}])")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def flatten(value: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        result: dict[str, str] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"invalid message key below {prefix or '<root>'}")
            result.update(flatten(child, f"{prefix}.{key}" if prefix else key))
        return result
    if not isinstance(value, str):
        raise ValueError(f"message {prefix} must be a string")
    return {prefix: value}


def validate_braces(key: str, message: str) -> None:
    depth = 0
    quoted = False
    index = 0
    while index < len(message):
        char = message[index]
        if char == "'":
            if index + 1 < len(message) and message[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted and char == "{":
            depth += 1
        elif not quoted and char == "}":
            depth -= 1
            if depth < 0:
                raise ValueError(f"{key}: unmatched closing brace")
        index += 1
    if depth:
        raise ValueError(f"{key}: unmatched opening brace")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_manifest() -> dict[str, Any]:
    manifest = json.loads((ROOT / "upstream" / "manifest.json").read_text())
    for section in ("patches",):
        for item in manifest[section]:
            path = ROOT / item["path"]
            expected = item["sha256"]
            if not SHA256_RE.fullmatch(expected) or file_sha256(path) != expected:
                raise ValueError(f"checksum mismatch: {path.relative_to(ROOT)}")
    for item in manifest["locales"].values():
        path = ROOT / item["path"]
        expected = item["sha256"]
        if not SHA256_RE.fullmatch(expected) or file_sha256(path) != expected:
            raise ValueError(f"checksum mismatch: {path.relative_to(ROOT)}")
    return manifest


def validate_resources(upstream: Path | None) -> None:
    en_path = ROOT / "locales" / "en.json"
    zh_path = ROOT / "locales" / "zh-CN.json"
    en = flatten(json.loads(en_path.read_text()))
    zh = flatten(json.loads(zh_path.read_text()))
    allowlist = json.loads((ROOT / "terminology-allowlist.json").read_text())

    missing = sorted(set(en) - set(zh))
    extra = sorted(set(zh) - set(en))
    if missing or extra:
        raise ValueError(f"locale key mismatch; missing={missing}, extra={extra}")

    for key in sorted(en):
        if not en[key].strip() or not zh[key].strip():
            raise ValueError(f"empty translation: {key}")
        validate_braces(key, en[key])
        validate_braces(key, zh[key])
        if set(PLACEHOLDER_RE.findall(en[key])) != set(
            PLACEHOLDER_RE.findall(zh[key])
        ):
            raise ValueError(f"placeholder mismatch: {key}")

    unknown_allowlist = sorted(set(allowlist) - set(en))
    if unknown_allowlist:
        raise ValueError(f"unknown terminology allowlist keys: {unknown_allowlist}")
    untranslated = {key for key in en if en[key] == zh[key]}
    unreviewed = sorted(untranslated - set(allowlist))
    if unreviewed:
        raise ValueError(f"unreviewed identical translations: {unreviewed}")

    translated_scope = set(en) - set(allowlist)
    translated = sum(en[key] != zh[key] for key in translated_scope)
    coverage = translated / len(translated_scope) if translated_scope else 1.0
    if coverage < 1:
        raise ValueError(f"translation coverage is {coverage:.1%}, expected 100%")

    if upstream:
        for locale_path in (en_path, zh_path):
            copied = upstream / "web" / "messages" / locale_path.name
            if not copied.is_file() or copied.read_bytes() != locale_path.read_bytes():
                raise ValueError(f"patched upstream locale differs: {copied}")
        for expected in (
            "web/src/features/i18n/config.ts",
            "web/src/features/i18n/LanguageSwitcher.tsx",
            "web/src/features/i18n/LocalizedUiLabel.tsx",
        ):
            if not (upstream / expected).is_file():
                raise ValueError(f"patched source is missing {expected}")
        manifest = json.loads((ROOT / "upstream" / "manifest.json").read_text())
        if file_sha256(upstream / "pnpm-lock.yaml") != manifest["source"][
            "patchedLockfileSha256"
        ]:
            raise ValueError("patched pnpm-lock.yaml does not match the release manifest")
        if file_sha256(upstream / "web" / "Dockerfile") != manifest["source"][
            "upstreamDockerfileSha256"
        ]:
            raise ValueError("upstream web/Dockerfile changed unexpectedly")

    print(
        f"i18n resources: OK ({len(en)} keys, zh-CN coverage {coverage:.1%}, "
        f"{len(allowlist)} reviewed terminology exceptions)"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", type=Path)
    args = parser.parse_args()
    try:
        validate_manifest()
        validate_resources(args.upstream.resolve() if args.upstream else None)
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"i18n validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
