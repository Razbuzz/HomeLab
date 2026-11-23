from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_FILE = REPO_ROOT / ".env"
README_FILE = REPO_ROOT / "README.md"
DASHBOARD_HTML = REPO_ROOT / "apps" / "home-server-dashboard" / "html" / "index.html"
SCRIPTS_DIR = REPO_ROOT / "scripts"
MAKEFILE = REPO_ROOT / "Makefile"

# Variables in .env that are intentionally kept for documentation / operator use
# even if not currently interpolated in docker-compose.yml, scripts, Makefile, or dashboard.
UNUSED_ENV_ALLOWLIST = {
    "DEFAULT_LAN_IP",
}

# Services listed in the dashboard web UI whose ports are not published via compose `ports:`
# because they either run with `network_mode: host` (plex) or directly on the host OS (cockpit).
DASHBOARD_HOST_PORT_ALLOWLIST = {
    "Cockpit": 9090,
    "Plex": 32400,
}


def parse_env_entries(env_text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for raw_line in env_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        assert "=" in line, f"Malformed .env line (missing '='): {raw_line!r}"
        key, value = line.split("=", 1)
        entries.append((key.strip(), value.strip()))
    return entries


def extract_readme_env_block(readme_text: str) -> str:
    match = re.search(r"```yml\s*\n(.*?)\n[ \t]*```", readme_text, re.DOTALL)
    assert match is not None, "Could not find a ```yml ... ``` .env example block in README.md"
    return match.group(1)


def parse_compose_yaml() -> dict:
    data = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "services" in data, "docker-compose.yml must define a top-level 'services' mapping"
    return data


def _substitute_env_vars(value: str, env_map: dict[str, str]) -> str:
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in env_map and env_map[name] != "":
            return env_map[name]
        return default if default is not None else ""

    return pattern.sub(replace, value)


def test_compose_validates_with_docker_compose() -> None:
    compose_data = parse_compose_yaml()
    assert compose_data["services"], "docker-compose.yml must define at least one service"

    if shutil.which("docker") is None:
        pytest.skip("docker binary not available in PATH")

    check = subprocess.run(
        ["docker", "compose", "version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode != 0:
        pytest.skip(f"docker compose plugin not available: {check.stderr.strip()}")

    env = os.environ.copy()
    env["DEFAULT_STORAGE_MOUNT"] = "/tmp/homelab-storage"
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"docker compose config --format json failed (exit {result.returncode}):\n"
        f"{result.stderr.strip()}"
    )
    parsed = json.loads(result.stdout)
    assert "services" in parsed and parsed["services"], "Rendered compose config must contain services"


def test_compose_vars_without_default_are_defined_in_env() -> None:
    env_keys = {k for k, _ in parse_env_entries(ENV_FILE.read_text(encoding="utf-8"))}
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")

    missing: list[str] = []
    for match in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::(-|\?)([^}]*))?\}", compose_text):
        var_name = match.group(1)
        operator = match.group(2)
        if operator == "-":
            continue
        if var_name not in env_keys and var_name not in missing:
            missing.append(var_name)

    assert not missing, (
        "Variables referenced in docker-compose.yml without a :-default are missing from .env: "
        + ", ".join(missing)
    )


def test_env_keys_are_used_across_repo() -> None:
    env_entries = parse_env_entries(ENV_FILE.read_text(encoding="utf-8"))
    searchable_files = [
        COMPOSE_FILE,
        MAKEFILE,
        DASHBOARD_HTML,
        *sorted(p for p in SCRIPTS_DIR.rglob("*") if p.is_file()),
    ]
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in searchable_files if p.exists())

    unused = [
        key
        for key, _ in env_entries
        if key not in UNUSED_ENV_ALLOWLIST and not re.search(rf"\b{re.escape(key)}\b", corpus)
    ]
    assert not unused, (
        "Keys defined in .env are not referenced in docker-compose.yml, scripts/, Makefile, or dashboard "
        f"(add intentional exceptions to UNUSED_ENV_ALLOWLIST): {', '.join(unused)}"
    )


def test_readme_env_block_mirrors_env_file() -> None:
    env_entries = parse_env_entries(ENV_FILE.read_text(encoding="utf-8"))
    readme_block = extract_readme_env_block(README_FILE.read_text(encoding="utf-8"))
    readme_entries = parse_env_entries(readme_block)

    assert readme_entries == env_entries, (
        "README.md ```yml .env example block does not mirror .env exactly (same KEY=VALUE lines in same order).\n"
        f"Only in .env or mismatched: {[item for item in env_entries if item not in readme_entries]}\n"
        f"Only in README.md or mismatched: {[item for item in readme_entries if item not in env_entries]}"
    )


def test_no_duplicate_published_host_ports() -> None:
    env_map = dict(parse_env_entries(ENV_FILE.read_text(encoding="utf-8")))
    env_map["DEFAULT_STORAGE_MOUNT"] = "/tmp/homelab-storage"
    compose = parse_compose_yaml()

    seen: dict[tuple[str, str], str] = {}
    duplicates: list[str] = []

    for service_name, service_cfg in compose["services"].items():
        for raw_port in service_cfg.get("ports") or []:
            port_str = _substitute_env_vars(str(raw_port), env_map)
            proto = "tcp"
            if "/" in port_str:
                port_part, proto = port_str.rsplit("/", 1)
            else:
                port_part = port_str
            parts = port_part.split(":")
            if len(parts) == 2:
                host_port = parts[0]
            elif len(parts) == 3:
                host_port = f"{parts[0]}:{parts[1]}" if parts[0] else parts[1]
            else:
                continue

            key = (host_port, proto.lower())
            if key in seen:
                duplicates.append(
                    f"host port {host_port}/{proto} published by both '{seen[key]}' and '{service_name}'"
                )
            else:
                seen[key] = service_name

    assert not duplicates, "Duplicate published host ports detected:\n" + "\n".join(duplicates)


def test_services_have_required_fields_and_unique_container_names() -> None:
    compose = parse_compose_yaml()
    services = compose["services"]
    container_names: dict[str, str] = {}

    for service_name, service_cfg in services.items():
        assert isinstance(service_cfg, dict), f"Service '{service_name}' must be a mapping"
        assert service_cfg.get("container_name"), f"Service '{service_name}' is missing 'container_name'"
        assert service_cfg.get("restart"), f"Service '{service_name}' is missing 'restart' policy"
        assert service_cfg.get("image") or service_cfg.get("build"), (
            f"Service '{service_name}' must define 'image' or 'build'"
        )

        cname = str(service_cfg["container_name"])
        assert cname not in container_names, (
            f"Duplicate container_name '{cname}' used by both '{container_names[cname]}' and '{service_name}'"
        )
        container_names[cname] = service_name


def test_dashboard_services_match_published_or_allowlisted_ports() -> None:
    env_map = dict(parse_env_entries(ENV_FILE.read_text(encoding="utf-8")))
    compose = parse_compose_yaml()

    published_ports: set[int] = set()
    for service_cfg in compose["services"].values():
        for raw_port in service_cfg.get("ports") or []:
            port_str = _substitute_env_vars(str(raw_port), env_map)
            port_part = port_str.rsplit("/", 1)[0]
            parts = port_part.split(":")
            host_port_str = parts[-2] if len(parts) >= 2 else ""
            if host_port_str.isdigit():
                published_ports.add(int(host_port_str))

    html_text = DASHBOARD_HTML.read_text(encoding="utf-8")
    array_match = re.search(r"const\s+services\s*=\s*\[(.*?)\];", html_text, re.DOTALL)
    assert array_match is not None, "Could not locate `const services = [...]` array in dashboard index.html"

    entries = re.findall(r'\{[^}]*name:\s*"([^"]+)"[^}]*port:\s*(null|\d+)[^}]*\}', array_match.group(1))
    assert entries, "No service entries parsed from dashboard index.html"

    bad_services: list[str] = []
    for name, port_raw in entries:
        if port_raw == "null":
            continue
        port = int(port_raw)
        if name in DASHBOARD_HOST_PORT_ALLOWLIST:
            expected_port = DASHBOARD_HOST_PORT_ALLOWLIST[name]
            if port != expected_port:
                bad_services.append(
                    f"dashboard service '{name}' uses port {port}, expected allowlisted host port {expected_port}"
                )
            continue
        if port not in published_ports:
            bad_services.append(
                f"dashboard service '{name}' uses port {port}, which is not published in docker-compose.yml "
                "or listed in DASHBOARD_HOST_PORT_ALLOWLIST"
            )

    assert not bad_services, "Dashboard service port mismatches found:\n" + "\n".join(bad_services)


def test_shell_scripts_use_bash_shebang_and_set_e() -> None:
    scripts = sorted(SCRIPTS_DIR.rglob("*.sh"))
    assert scripts, "Expected shell scripts under scripts/"

    issues: list[str] = []
    for script in scripts:
        rel = script.relative_to(REPO_ROOT)
        lines = script.read_text(encoding="utf-8").splitlines()
        first_line = lines[0] if lines else "empty"
        if not lines or lines[0].strip() not in {"#!/bin/bash", "#!/usr/bin/env bash"}:
            issues.append(f"{rel}: first line must be a bash shebang (found {first_line!r})")
        has_set_e = any(
            re.match(r"^\s*set\s+-[a-zA-Z]*e[a-zA-Z]*\b", line)
            or re.match(r"^\s*set\s+-o\s+errexit\b", line)
            for line in lines[1:]
        )
        if not has_set_e:
            issues.append(f"{rel}: missing `set -e` (or `set -euo pipefail`)")

    assert not issues, "Shell script safety check failed:\n" + "\n".join(issues)
