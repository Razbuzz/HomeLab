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


# Matches either a Compose `$$` escape or a `${VAR...}` interpolation.
_COMPOSE_VAR_TOKEN_RE = re.compile(
    r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])([^}]*))?\}"
)

# Unspecified / wildcard bind addresses collide across all interfaces.
_WILDCARD_HOST_IPS = {"", "0.0.0.0", "::", "[::]"}


def _build_ci_env_map() -> dict[str, str]:
    env_map = dict(parse_env_entries(ENV_FILE.read_text(encoding="utf-8")))
    env_map["DEFAULT_STORAGE_MOUNT"] = "/tmp/homelab-storage"
    env_map["DEFAULT_LAN_IP"] = "127.0.0.1"
    return env_map


def _substitute_env_vars(value: str, env_map: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"

        name = match.group(1)
        op = match.group(2)
        arg = match.group(3) if match.group(3) is not None else ""
        is_set = name in env_map
        is_non_empty = is_set and env_map[name] != ""

        if op is None:
            return env_map.get(name, "")
        if op == ":-":
            return env_map[name] if is_non_empty else _substitute_env_vars(arg, env_map)
        if op == "-":
            return env_map[name] if is_set else _substitute_env_vars(arg, env_map)
        if op == ":+":
            return _substitute_env_vars(arg, env_map) if is_non_empty else ""
        if op == "+":
            return _substitute_env_vars(arg, env_map) if is_set else ""
        if op == ":?":
            if not is_non_empty:
                raise AssertionError(f"Required Compose variable '{name}' is unset or empty: {arg}")
            return env_map[name]
        if op == "?":
            if not is_set:
                raise AssertionError(f"Required Compose variable '{name}' is unset: {arg}")
            return env_map[name]
        return ""

    return _COMPOSE_VAR_TOKEN_RE.sub(replace, value)


def _find_required_compose_vars(compose_text: str) -> list[str]:
    required: list[str] = []
    for match in _COMPOSE_VAR_TOKEN_RE.finditer(compose_text):
        if match.group(0) == "$$":
            continue
        var_name = match.group(1)
        op = match.group(2)
        # Only bare `${VAR}` and error-on-missing `${VAR:?...}` / `${VAR?...}` require
        # `.env` definition; `${VAR:-default}`, `${VAR-default}`, `${VAR:+alt}`, `${VAR+alt}`
        # all have fallback/conditional behavior when unset.
        if op in (None, ":?", "?") and var_name not in required:
            required.append(var_name)
    return required


def _normalize_host_ip(host_ip: str | None) -> str:
    if host_ip is None:
        return ""
    cleaned = host_ip.strip()
    if cleaned in _WILDCARD_HOST_IPS:
        return ""
    if cleaned.startswith("[") and cleaned.endswith("]"):
        inner = cleaned[1:-1]
        if inner in {"", "::"}:
            return ""
        return inner.lower()
    return cleaned.lower()


def _expand_port_range(port_spec: str | int) -> list[int]:
    spec = str(port_spec).strip()
    if not spec:
        return []
    if "-" in spec:
        start_str, end_str = spec.split("-", 1)
        assert start_str.isdigit() and end_str.isdigit(), f"Invalid port range: {port_spec!r}"
        start, end = int(start_str), int(end_str)
        assert start <= end, f"Descending port range is invalid: {port_spec!r}"
        return list(range(start, end + 1))
    assert spec.isdigit(), f"Invalid port specification: {port_spec!r}"
    return [int(spec)]


def _split_host_ip_and_port(host_part: str) -> tuple[str, str]:
    """Split `<host_ip>:<host_port>` supporting bracketed IPv6 (`[::1]:8080`) and IPv4."""
    if host_part.startswith("["):
        closing = host_part.find("]")
        assert closing != -1, f"Malformed IPv6 host binding: {host_part!r}"
        host_ip = host_part[: closing + 1]
        rest = host_part[closing + 1 :]
        if rest == "":
            return "", host_ip
        assert rest.startswith(":"), f"Unexpected host binding syntax after IPv6 literal: {host_part!r}"
        return _normalize_host_ip(host_ip), rest[1:]
    if ":" in host_part:
        host_ip, host_port = host_part.split(":", 1)
        return _normalize_host_ip(host_ip), host_port
    return "", host_part


def _parse_compose_port_bindings(
    raw_port: str | int | dict, env_map: dict[str, str]
) -> list[tuple[str, int, str]]:
    """Return list of `(normalized_host_ip, host_port, protocol)` for a Compose `ports:` entry."""
    if isinstance(raw_port, dict):
        published = raw_port.get("published")
        if published in (None, ""):
            return []
        published_str = _substitute_env_vars(str(published), env_map).strip()
        if not published_str:
            return []
        host_ip_raw = raw_port.get("host_ip")
        host_ip = (
            _normalize_host_ip(_substitute_env_vars(str(host_ip_raw), env_map))
            if host_ip_raw is not None
            else ""
        )
        proto_raw = raw_port.get("protocol", "tcp")
        proto = _substitute_env_vars(str(proto_raw), env_map).strip().lower() or "tcp"
        return [(host_ip, port, proto) for port in _expand_port_range(published_str)]

    port_str = _substitute_env_vars(str(raw_port), env_map).strip()
    if not port_str:
        return []

    proto = "tcp"
    if "/" in port_str:
        port_part, proto_part = port_str.rsplit("/", 1)
        proto = proto_part.strip().lower() or "tcp"
    else:
        port_part = port_str

    # Short syntax forms:
    # - CONTAINER (no published host port; ephemeral host port)
    # - HOST:CONTAINER
    # - IP:HOST:CONTAINER
    # - [IPv6]:HOST:CONTAINER
    # - IP::CONTAINER (ephemeral host port on specific IP)
    if ":" not in port_part:
        return []

    host_side, _container_side = port_part.rsplit(":", 1)
    host_ip, host_port_spec = _split_host_ip_and_port(host_side)
    if not host_port_spec.strip():
        return []

    return [(host_ip, port, proto) for port in _expand_port_range(host_port_spec)]


def _host_bindings_conflict(ip_a: str, ip_b: str) -> bool:
    """Wildcard bind (`""`) collides with any bind on the same port+protocol."""
    return ip_a == "" or ip_b == "" or ip_a == ip_b


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
    env["DEFAULT_LAN_IP"] = "127.0.0.1"
    result = subprocess.run(
        ["docker", "compose", "--profile", "*", "config", "--format", "json"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"docker compose --profile '*' config --format json failed (exit {result.returncode}):\n"
        f"{result.stderr.strip()}"
    )
    parsed = json.loads(result.stdout)
    assert "services" in parsed and parsed["services"], "Rendered compose config must contain services"


def test_compose_vars_without_default_are_defined_in_env() -> None:
    env_keys = {k for k, _ in parse_env_entries(ENV_FILE.read_text(encoding="utf-8"))}
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")

    missing = [
        var_name
        for var_name in _find_required_compose_vars(compose_text)
        if var_name not in env_keys
    ]

    assert not missing, (
        "Variables referenced in docker-compose.yml without a default are missing from .env: "
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
    env_map = _build_ci_env_map()
    compose = parse_compose_yaml()

    seen: dict[tuple[int, str], list[tuple[str, str]]] = {}
    duplicates: list[str] = []

    for service_name, service_cfg in compose["services"].items():
        for raw_port in service_cfg.get("ports") or []:
            for host_ip, host_port, proto in _parse_compose_port_bindings(raw_port, env_map):
                port_key = (host_port, proto)
                existing_bindings = seen.setdefault(port_key, [])
                for existing_ip, existing_service in existing_bindings:
                    if _host_bindings_conflict(existing_ip, host_ip):
                        bind_a = existing_ip or "*"
                        bind_b = host_ip or "*"
                        duplicates.append(
                            f"host port {host_port}/{proto} conflicts between "
                            f"'{existing_service}' ({bind_a}) and '{service_name}' ({bind_b})"
                        )
                existing_bindings.append((host_ip, service_name))

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


def test_all_services_define_healthcheck() -> None:
    compose = parse_compose_yaml()
    services = compose["services"]
    issues: list[str] = []

    for service_name, service_cfg in services.items():
        if not isinstance(service_cfg, dict):
            issues.append(f"{service_name}: service definition must be a mapping")
            continue
        healthcheck = service_cfg.get("healthcheck")
        if not isinstance(healthcheck, dict):
            issues.append(f"{service_name}: missing 'healthcheck' mapping")
            continue
        if healthcheck.get("disable"):
            issues.append(f"{service_name}: healthcheck must not be disabled")
        for field in ("test", "interval", "timeout", "retries"):
            if not healthcheck.get(field):
                issues.append(f"{service_name}: healthcheck is missing non-empty '{field}'")

    assert not issues, "Services missing valid healthcheck definitions:\n" + "\n".join(issues)


def test_dashboard_services_match_published_or_allowlisted_ports() -> None:
    env_map = _build_ci_env_map()
    compose = parse_compose_yaml()

    published_ports: set[int] = set()
    for service_cfg in compose["services"].values():
        for raw_port in service_cfg.get("ports") or []:
            for _host_ip, host_port, _proto in _parse_compose_port_bindings(raw_port, env_map):
                published_ports.add(host_port)

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


def test_compose_helpers_handle_escapes_defaults_and_port_variants() -> None:
    env = {"SET_EMPTY": "", "SET_VAL": "1234", "LAN_IP": "192.168.1.50"}

    # $$ literal escape must not interpolate as an env var
    assert _substitute_env_vars("$${SET_VAL} $$(cmd)", env) == "${SET_VAL} $(cmd)"
    assert _find_required_compose_vars(
        "$${LITERAL} ${OPT_COLON:-x} ${OPT_NO_COLON-y} ${ALT_COLON:+z} ${ALT_NO_COLON+w} ${REQ} ${REQ_ERR:?err} ${REQ_ERR2?err}"
    ) == ["REQ", "REQ_ERR", "REQ_ERR2"]

    # :- vs - and :+ vs +
    assert _substitute_env_vars("${SET_EMPTY:-fallback}", env) == "fallback"
    assert _substitute_env_vars("${SET_EMPTY-fallback}", env) == ""
    assert _substitute_env_vars("${UNSET_VAR-fallback}", env) == "fallback"
    assert _substitute_env_vars("${SET_EMPTY:+alt}", env) == ""
    assert _substitute_env_vars("${SET_EMPTY+alt}", env) == "alt"
    assert _substitute_env_vars("${SET_VAL:+alt}", env) == "alt"

    # Short syntax: container-only, host:container, IPv4, IPv6, ranges, protocols
    assert _parse_compose_port_bindings("80", env) == []
    assert _parse_compose_port_bindings("8080:80", env) == [("", 8080, "tcp")]
    assert _parse_compose_port_bindings("0.0.0.0:8080:80", env) == [("", 8080, "tcp")]
    assert _parse_compose_port_bindings("[::]:8080:80", env) == [("", 8080, "tcp")]
    assert _parse_compose_port_bindings("[::1]:8080:80/udp", env) == [("::1", 8080, "udp")]
    assert _parse_compose_port_bindings("${LAN_IP}:${SET_VAL}:9119", env) == [("192.168.1.50", 1234, "tcp")]
    assert _parse_compose_port_bindings("8000-8002:8000-8002", env) == [
        ("", 8000, "tcp"),
        ("", 8001, "tcp"),
        ("", 8002, "tcp"),
    ]
    assert _parse_compose_port_bindings(
        {"target": 80, "published": "8080-8081", "host_ip": "127.0.0.1", "protocol": "udp"},
        env,
    ) == [("127.0.0.1", 8080, "udp"), ("127.0.0.1", 8081, "udp")]

    assert _host_bindings_conflict("", "127.0.0.1") is True
    assert _host_bindings_conflict("127.0.0.1", "127.0.0.1") is True
    assert _host_bindings_conflict("127.0.0.1", "192.168.1.50") is False


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
