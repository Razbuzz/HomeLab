#!/usr/bin/env python3
"""Safely merge an existing HomeLab .env file with a new .env template.

Preserves user values verbatim (without shell expansion), performs safe
key migrations (e.g. JELLYSEERR_* -> SEERR_*), adopts newly introduced
template defaults, and retains removed or custom user variables in a
commented footer so nothing is ever lost during upgrades.

Security note: never prints secret values to stdout or stderr.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLACEHOLDER_PREFIX = "CHANGE_TO_"
BETA_OPTIONAL_PREFIXES = ("HERMES_",)
VLLM_OPTIONAL_PREFIXES = ("VLLM_",)
OPTIONAL_PROFILE_PREFIXES = (*BETA_OPTIONAL_PREFIXES, *VLLM_OPTIONAL_PREFIXES)
OPTIONAL_ALLOW_PLACEHOLDER_KEYS = frozenset({"HERMES_OPENROUTER_API_KEY", "VLLM_HF_TOKEN"})
BETA_ALLOW_PLACEHOLDER_KEYS = OPTIONAL_ALLOW_PLACEHOLDER_KEYS

# (old_key, new_key)
KEY_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("JELLYSEERR_FOLDER", "SEERR_FOLDER"),
    ("JELLYSEERR_PORT", "SEERR_PORT"),
)


def parse_env_assignment(raw_line: str) -> tuple[str, str] | None:
    """Parse a KEY=VALUE line safely without shell evaluation or expansion."""
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        candidate = stripped[len("export ") :].lstrip()
        if "=" in candidate:
            key_part = candidate.split("=", 1)[0].strip()
            if KEY_RE.match(key_part):
                stripped = candidate
    if "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    key = key.strip()
    if not KEY_RE.match(key):
        return None
    value = value.strip()
    return key, value


def unquote_for_check(value: str) -> str:
    """Strip matching surrounding quotes solely for placeholder checking."""
    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"')
        or (value[0] == "'" and value[-1] == "'")
    ):
        return value[1:-1]
    return value


def is_placeholder(value: str | None) -> bool:
    """Return True if value is None/empty or begins with CHANGE_TO_."""
    if value is None:
        return True
    cleaned = unquote_for_check(value.strip())
    return cleaned == "" or cleaned.startswith(PLACEHOLDER_PREFIX)


def load_current_env(path: Path) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Return ordered unique (key, value) pairs (last wins) and lookup dict."""
    raw_pairs: list[tuple[str, str]] = []
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_env_assignment(line)
        if parsed is None:
            continue
        key, value = parsed
        raw_pairs.append((key, value))
        values[key] = value

    # Keep only the final occurrence per key in its last position
    seen: set[str] = set()
    ordered_rev: list[tuple[str, str]] = []
    for key, value in reversed(raw_pairs):
        if key not in seen:
            seen.add(key)
            ordered_rev.append((key, value))
    ordered_rev.reverse()
    return ordered_rev, values


def merge_env_texts(
    template_text: str,
    current_pairs: list[tuple[str, str]],
    current_values: dict[str, str],
) -> tuple[str, dict[str, list[str]]]:
    """Merge template_text with current_values and return (output_text, stats)."""
    template_lines = template_text.splitlines()
    template_keys: set[str] = set()
    for line in template_lines:
        parsed = parse_env_assignment(line)
        if parsed is not None:
            template_keys.add(parsed[0])

    # Determine applicable migrations
    migrated_sources: set[str] = set()
    migrated_targets: dict[str, tuple[str, str]] = {}
    for old_key, new_key in KEY_MIGRATIONS:
        if new_key not in template_keys:
            continue
        if old_key not in current_values:
            continue
        old_val = current_values[old_key]
        if is_placeholder(old_val):
            continue
        existing_new_val = current_values.get(new_key)
        if new_key not in current_values or is_placeholder(existing_new_val):
            migrated_targets[new_key] = (old_key, old_val)
            migrated_sources.add(old_key)

    out_lines: list[str] = []
    preserved_keys: list[str] = []
    added_keys: list[str] = []
    migrated_keys: list[str] = []
    active_values: dict[str, str] = {}

    for line in template_lines:
        parsed = parse_env_assignment(line)
        if parsed is None:
            out_lines.append(line)
            continue

        key, template_val = parsed
        if key in migrated_targets:
            old_key, old_val = migrated_targets[key]
            out_lines.append(f"{key}={old_val}")
            active_values[key] = old_val
            migrated_keys.append(f"{old_key}->{key}")
        elif key in current_values:
            user_val = current_values[key]
            out_lines.append(f"{key}={user_val}")
            active_values[key] = user_val
            preserved_keys.append(key)
        else:
            out_lines.append(f"{key}={template_val}")
            active_values[key] = template_val
            added_keys.append(key)

    # Preserve non-template user variables (removed services, custom overrides, etc.)
    # Do not duplicate keys that were already migrated into active template keys.
    legacy_pairs = [
        (k, v)
        for k, v in current_pairs
        if k not in template_keys and k not in migrated_sources
    ]

    if legacy_pairs:
        if out_lines and out_lines[-1] != "":
            out_lines.append("")
        out_lines.append("# Preserved legacy / custom variables")
        for key, value in legacy_pairs:
            out_lines.append(f"{key}={value}")
            active_values[key] = value

    output_text = "\n".join(out_lines) + "\n"
    stats = {
        "preserved": preserved_keys,
        "added": added_keys,
        "migrated": migrated_keys,
        "legacy": [k for k, _ in legacy_pairs],
        "active_values": active_values,  # type: ignore[dict-item]
    }
    return output_text, stats


def find_unresolved_placeholders(
    template_text: str,
    active_values: dict[str, str],
    include_beta: bool,
    include_vllm: bool = False,
) -> list[str]:
    """Return template keys still set to CHANGE_TO_* placeholders."""
    unresolved: list[str] = []
    for line in template_text.splitlines():
        parsed = parse_env_assignment(line)
        if parsed is None:
            continue
        key, _ = parsed
        if not include_beta and key.startswith(BETA_OPTIONAL_PREFIXES):
            continue
        if not include_vllm and key.startswith(VLLM_OPTIONAL_PREFIXES):
            continue
        if key in OPTIONAL_ALLOW_PLACEHOLDER_KEYS:
            continue
        val = active_values.get(key, "")
        cleaned = unquote_for_check(val.strip())
        if cleaned.startswith(PLACEHOLDER_PREFIX):
            unresolved.append(key)
    return unresolved


def write_atomic_0600(output_path: Path, content: str) -> None:
    """Write content atomically to output_path with strict 0600 permissions."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.tmp.",
        dir=str(output_path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        os.chmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, output_path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely merge an existing HomeLab .env with a new .env template."
    )
    parser.add_argument(
        "--template",
        required=True,
        type=Path,
        help="Path to the target/new .env template file.",
    )
    parser.add_argument(
        "--current",
        required=True,
        type=Path,
        help="Path to the operator's current .env file.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Destination path for the merged .env file.",
    )
    parser.add_argument(
        "--beta",
        action="store_true",
        help="Also enforce placeholder checks for beta profile variables (HERMES_*).",
    )
    parser.add_argument(
        "--vllm",
        action="store_true",
        help="Also enforce placeholder checks for opt-in vLLM profile variables (VLLM_*).",
    )
    parser.add_argument(
        "--check-placeholders",
        action="store_true",
        help="Fail non-zero if any required active key still uses a CHANGE_TO_* placeholder.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview merge summary and placeholder checks without writing --output.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.template.is_file():
        print(f"Error: template .env not found: {args.template}", file=sys.stderr)
        return 1
    if not args.current.is_file():
        print(f"Error: current .env not found: {args.current}", file=sys.stderr)
        return 1

    template_text = args.template.read_text(encoding="utf-8")
    current_pairs, current_values = load_current_env(args.current)
    merged_text, stats = merge_env_texts(template_text, current_pairs, current_values)

    preserved: list[str] = stats["preserved"]
    added: list[str] = stats["added"]
    migrated: list[str] = stats["migrated"]
    legacy: list[str] = stats["legacy"]
    active_values: dict[str, str] = stats["active_values"]  # type: ignore[assignment]

    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}.env merge summary: "
        f"preserved={len(preserved)}, added={len(added)}, "
        f"migrated={len(migrated)}, legacy={len(legacy)}"
    )
    if added:
        print(f"{prefix}  added keys: {', '.join(added)}")
    if migrated:
        print(f"{prefix}  migrated keys: {', '.join(migrated)}")
    if legacy:
        print(f"{prefix}  preserved legacy/custom keys: {', '.join(legacy)}")

    unresolved = find_unresolved_placeholders(
        template_text, active_values, include_beta=args.beta, include_vllm=args.vllm
    )
    if args.check_placeholders and unresolved:
        print(
            f"Error: unresolved CHANGE_TO_* placeholders in required keys: "
            f"{', '.join(unresolved)}",
            file=sys.stderr,
        )
        return 2

    if unresolved:
        print(
            f"{prefix}  note: unresolved CHANGE_TO_* placeholders: {', '.join(unresolved)}"
        )

    if not args.dry_run:
        write_atomic_0600(args.output, merged_text)
        print(f"Wrote merged .env to {args.output} (mode 0600)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
