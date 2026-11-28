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
CI_COMPOSE_FILE = REPO_ROOT / "tests" / "docker-compose.ci.yml"
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


class _ComposeOverrideSafeLoader(yaml.SafeLoader):
    """YAML SafeLoader supporting Docker Compose `!reset` and `!override` tags."""


def _compose_tag_constructor(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> object:
    del tag_suffix
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


_ComposeOverrideSafeLoader.add_multi_constructor("!", _compose_tag_constructor)


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


def test_ci_compose_override_is_valid_and_scoped() -> None:
    base_compose = parse_compose_yaml()
    assert CI_COMPOSE_FILE.is_file(), "tests/docker-compose.ci.yml must exist"

    override_data = yaml.load(
        CI_COMPOSE_FILE.read_text(encoding="utf-8"),
        Loader=_ComposeOverrideSafeLoader,
    )
    assert isinstance(override_data, dict) and isinstance(override_data.get("services"), dict), (
        "tests/docker-compose.ci.yml must define a top-level 'services' mapping"
    )

    unknown_services = sorted(set(override_data["services"]) - set(base_compose["services"]))
    assert not unknown_services, (
        "tests/docker-compose.ci.yml defines unknown services not in docker-compose.yml: "
        + ", ".join(unknown_services)
    )

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
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "-f",
            str(CI_COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"docker compose -f docker-compose.yml -f tests/docker-compose.ci.yml config --format json failed "
        f"(exit {result.returncode}):\n{result.stderr.strip()}"
    )
    parsed = json.loads(result.stdout)
    services = parsed["services"]

    for gpu_service in ("plex", "ollama"):
        svc_cfg = services[gpu_service]
        assert svc_cfg.get("runtime") is None, f"{gpu_service} CI override must strip runtime"
        assert svc_cfg.get("deploy") is None, f"{gpu_service} CI override must strip deploy GPU reservation"

    plex_sources = [vol.get("source") for vol in services["plex"].get("volumes") or []]
    assert "/dev/dri" not in plex_sources, "plex CI override must strip /dev/dri device mount"

    pihole_ports = services["pihole"].get("ports") or []
    assert all(str(p.get("published")) != "53" for p in pihole_ports), (
        "pihole CI override must replace (not merge) privileged host port 53"
    )
    for proto in ("tcp", "udp"):
        assert any(
            str(p.get("published")) == "15353"
            and p.get("target") == 53
            and str(p.get("protocol", "")).lower() == proto
            for p in pihole_ports
        ), f"pihole CI override must remap host port 53/{proto} to 15353/{proto}"


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
        *sorted(
            p
            for p in SCRIPTS_DIR.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        ),
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


def _validate_service_healthcheck(service_name: str, service_cfg: dict) -> list[str]:
    if not isinstance(service_cfg, dict):
        return [f"{service_name}: service definition must be a mapping"]

    healthcheck = service_cfg.get("healthcheck")
    if not isinstance(healthcheck, dict):
        return [f"{service_name}: missing 'healthcheck' mapping"]

    issues: list[str] = []
    if healthcheck.get("disable"):
        issues.append(f"{service_name}: healthcheck must not be disabled")

    test = healthcheck.get("test")
    if isinstance(test, str):
        stripped_test = test.strip()
        if not stripped_test or stripped_test.upper() == "NONE":
            issues.append(f"{service_name}: healthcheck.test must be a non-empty active command")
    elif isinstance(test, list):
        if not test or not all(isinstance(item, str) for item in test):
            issues.append(f"{service_name}: healthcheck.test list must be a non-empty list of strings")
        else:
            kind = test[0].strip()
            if kind.upper() == "NONE":
                issues.append(f"{service_name}: healthcheck.test must not be 'NONE'")
            elif kind == "CMD":
                if len(test) < 2 or not test[1].strip():
                    issues.append(
                        f"{service_name}: healthcheck.test CMD form requires a non-empty command"
                    )
            elif kind == "CMD-SHELL":
                if len(test) != 2 or not test[1].strip():
                    issues.append(
                        f"{service_name}: healthcheck.test CMD-SHELL form requires a single non-empty command string"
                    )
            else:
                issues.append(
                    f"{service_name}: healthcheck.test list must start with 'CMD' or 'CMD-SHELL' (got {test[0]!r})"
                )
    else:
        issues.append(f"{service_name}: healthcheck.test must be a command string or list")

    for duration_field in ("interval", "timeout"):
        value = healthcheck.get(duration_field)
        if isinstance(value, str):
            if not value.strip():
                issues.append(f"{service_name}: healthcheck.{duration_field} must not be blank")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if value <= 0:
                issues.append(f"{service_name}: healthcheck.{duration_field} must be positive")
        else:
            issues.append(
                f"{service_name}: healthcheck.{duration_field} must be a non-empty duration string or positive number"
            )

    retries = healthcheck.get("retries")
    if not isinstance(retries, int) or isinstance(retries, bool) or retries <= 0:
        issues.append(f"{service_name}: healthcheck.retries must be a positive integer")

    return issues


def test_all_services_define_healthcheck() -> None:
    compose = parse_compose_yaml()
    services = compose["services"]
    issues: list[str] = []

    for service_name, service_cfg in services.items():
        issues.extend(_validate_service_healthcheck(service_name, service_cfg))

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


def test_validate_service_healthcheck_rejects_disabled_and_malformed_configs() -> None:
    valid_cmd = {
        "healthcheck": {
            "test": ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:80/"],
            "interval": "30s",
            "timeout": "5s",
            "retries": 3,
        }
    }
    valid_shell = {
        "healthcheck": {
            "test": ["CMD-SHELL", "curl -sf http://127.0.0.1:80/health || exit 1"],
            "interval": "30s",
            "timeout": "5s",
            "retries": 3,
        }
    }
    valid_str = {
        "healthcheck": {
            "test": "curl -sf http://127.0.0.1:80/health || exit 1",
            "interval": "30s",
            "timeout": "5s",
            "retries": 3,
        }
    }
    assert _validate_service_healthcheck("svc", valid_cmd) == []
    assert _validate_service_healthcheck("svc", valid_shell) == []
    assert _validate_service_healthcheck("svc", valid_str) == []

    base_hc = {"test": ["CMD-SHELL", "true"], "interval": "30s", "timeout": "5s", "retries": 3}

    invalid_cases: list[object] = [
        "not-a-dict",
        {},
        {"healthcheck": None},
        {"healthcheck": "disabled"},
        {"healthcheck": {**base_hc, "disable": True}},
        {"healthcheck": {**base_hc, "test": ["NONE"]}},
        {"healthcheck": {**base_hc, "test": ["none"]}},
        {"healthcheck": {**base_hc, "test": "NONE"}},
        {"healthcheck": {**base_hc, "test": ""}},
        {"healthcheck": {**base_hc, "test": "   "}},
        {"healthcheck": {**base_hc, "test": []}},
        {"healthcheck": {**base_hc, "test": [""]}},
        {"healthcheck": {**base_hc, "test": ["CMD"]}},
        {"healthcheck": {**base_hc, "test": ["CMD", "   "]}},
        {"healthcheck": {**base_hc, "test": ["CMD-SHELL"]}},
        {"healthcheck": {**base_hc, "test": ["CMD-SHELL", ""]}},
        {"healthcheck": {**base_hc, "test": ["CMD-SHELL", "   "]}},
        {"healthcheck": {**base_hc, "test": ["INVALID", "echo"]}},
        {"healthcheck": {**base_hc, "test": None}},
        {"healthcheck": {**base_hc, "test": 123}},
        {"healthcheck": {**base_hc, "interval": ""}},
        {"healthcheck": {**base_hc, "interval": "   "}},
        {"healthcheck": {**base_hc, "timeout": ""}},
        {"healthcheck": {**base_hc, "timeout": "   "}},
        {"healthcheck": {**base_hc, "timeout": True}},
        {"healthcheck": {**base_hc, "retries": 0}},
        {"healthcheck": {**base_hc, "retries": -1}},
        {"healthcheck": {**base_hc, "retries": True}},
        {"healthcheck": {**base_hc, "retries": "3"}},
    ]

    for idx, bad_cfg in enumerate(invalid_cases):
        errors = _validate_service_healthcheck(f"bad_{idx}", bad_cfg)  # type: ignore[arg-type]
        assert errors, f"Expected validation error for case #{idx}: {bad_cfg!r}"


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


def test_qa_test_parses_dashboard_services() -> None:
    import importlib.util
    import sys

    qa_script = SCRIPTS_DIR / "qa-test.py"
    assert qa_script.is_file(), "scripts/qa-test.py must exist"
    assert os.access(qa_script, os.X_OK), "scripts/qa-test.py must be executable"

    spec = importlib.util.spec_from_file_location("qa_test", qa_script)
    assert spec is not None and spec.loader is not None
    qa_mod = importlib.util.module_from_spec(spec)
    prev_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.modules["qa_test"] = qa_mod
    try:
        spec.loader.exec_module(qa_mod)
    finally:
        sys.modules.pop("qa_test", None)
        sys.dont_write_bytecode = prev_dont_write

    services = qa_mod.parse_dashboard_services(DASHBOARD_HTML.read_text(encoding="utf-8"))
    by_name = {entry["name"]: entry for entry in services}

    assert len(services) == 16, f"Expected 16 dashboard service cards, parsed {len(services)}: {list(by_name)}"
    assert by_name["Hermes Agent"]["port"] == 9119
    assert by_name["Hermes Agent"]["note"] == "Beta"
    assert by_name["Pi-hole"]["port"] == 12120
    assert by_name["Pi-hole"]["path"] == "/admin"
    assert by_name["Plex"]["port"] == 32400
    assert by_name["Plex"]["path"] == "/web"
    assert by_name["CouchDB"]["port"] == 5984
    assert by_name["CouchDB"]["path"] == "/_utils"
    assert by_name["Samba"]["port"] is None


def _load_review_gate_module():
    import importlib.util
    import sys

    script = SCRIPTS_DIR / "check-review-gate.py"
    assert script.is_file(), "scripts/check-review-gate.py must exist"
    assert os.access(script, os.X_OK), "scripts/check-review-gate.py must be executable"

    spec = importlib.util.spec_from_file_location("check_review_gate", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    prev_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.modules["check_review_gate"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("check_review_gate", None)
        sys.dont_write_bytecode = prev_dont_write
    return mod


def test_review_gate_evaluation_rules() -> None:
    mod = _load_review_gate_module()
    evaluate = mod.evaluate_reviews
    head_sha = "abcdef1234567890abcdef1234567890abcdef12"
    old_sha = "1111111222222233333334444444555555566666"
    substantive_approval = (
        "APPROVED\n\nVerified make check, CI workflows, and security settings cleanly."
    )

    # 1. No reviews -> blocked
    approved, msg = evaluate([], head_sha)
    assert approved is False
    assert "Awaiting full review approval" in msg
    assert head_sha[:7] in msg

    # 2. Stale APPROVED on an older commit_id -> ignored / blocked
    approved, msg = evaluate(
        [
            {
                "id": 1,
                "commit_id": old_sha,
                "state": "APPROVED",
                "body": substantive_approval,
                "submitted_at": "2026-11-28T10:00:00Z",
            }
        ],
        head_sha,
    )
    assert approved is False
    assert "Awaiting full review approval" in msg

    # 3. Native CHANGES_REQUESTED -> blocked
    approved, msg = evaluate(
        [
            {
                "id": 2,
                "commit_id": head_sha,
                "state": "CHANGES_REQUESTED",
                "body": "Please fix parity issue.",
                "submitted_at": "2026-11-28T10:05:00Z",
            }
        ],
        head_sha,
    )
    assert approved is False
    assert "Changes requested" in msg

    # 4. COMMENTED "CHANGES REQUESTED" -> blocked (including markdown wrapper)
    approved, msg = evaluate(
        [
            {
                "id": 3,
                "commit_id": head_sha,
                "state": "COMMENTED",
                "body": "**CHANGES REQUESTED**\n\nMissing healthcheck on new service.",
                "submitted_at": "2026-11-28T10:06:00Z",
            }
        ],
        head_sha,
    )
    assert approved is False
    assert "Changes requested" in msg

    # 5. APPROVED followed chronologically by CHANGES REQUESTED on same head_sha -> blocked
    approved, msg = evaluate(
        [
            {
                "id": 10,
                "commit_id": head_sha,
                "state": "COMMENTED",
                "body": "**CHANGES REQUESTED**\n\nFound a regression in workflow permissions.",
                "submitted_at": "2026-11-28T10:20:00Z",
            },
            {
                "id": 9,
                "commit_id": head_sha,
                "state": "APPROVED",
                "body": substantive_approval,
                "submitted_at": "2026-11-28T10:10:00Z",
            },
        ],
        head_sha,
    )
    assert approved is False
    assert "Changes requested" in msg

    # 6. CHANGES REQUESTED followed chronologically by substantive APPROVED on same head_sha -> approved
    approved, msg = evaluate(
        [
            {
                "id": 11,
                "commit_id": head_sha,
                "state": "CHANGES_REQUESTED",
                "body": "Please add unit tests.",
                "submitted_at": "2026-11-28T10:10:00Z",
            },
            {
                "id": 12,
                "commit_id": head_sha,
                "state": "COMMENTED",
                "body": substantive_approval,
                "submitted_at": "2026-11-28T10:25:00Z",
            },
        ],
        head_sha,
    )
    assert approved is True
    assert "Full review approved" in msg

    # 7. Trivial short COMMENTED "APPROVED" (< 40 chars) -> blocked
    approved, msg = evaluate(
        [
            {
                "id": 13,
                "commit_id": head_sha,
                "state": "COMMENTED",
                "body": "APPROVED",
                "submitted_at": "2026-11-28T10:30:00Z",
            }
        ],
        head_sha,
    )
    assert approved is False
    assert "too brief" in msg.lower()

    # 8. Substantive COMMENTED review starting with APPROVED / **Verdict:** APPROVED -> approved
    for verdict_line in (
        "APPROVED",
        "**Verdict:** APPROVED",
        "### Verdict: APPROVED",
    ):
        body = f"{verdict_line}\n\nReviewed diff, parity, and full test output."
        assert len(body.strip()) >= 40
        approved, msg = evaluate(
            [
                {
                    "id": 14,
                    "commit_id": head_sha,
                    "state": "COMMENTED",
                    "body": body,
                    "submitted_at": "2026-11-28T10:35:00Z",
                }
            ],
            head_sha,
        )
        assert approved is True, f"Expected approval for {verdict_line!r}, got {msg}"

    # 8b. **Verdict:** CHANGES_REQUESTED wrapper -> blocked
    approved, msg = evaluate(
        [
            {
                "id": 141,
                "commit_id": head_sha,
                "state": "COMMENTED",
                "body": "**Verdict:** CHANGES_REQUESTED\n\nPlease fix port collision in compose.",
                "submitted_at": "2026-11-28T10:36:00Z",
            }
        ],
        head_sha,
    )
    assert approved is False
    assert "Changes requested" in msg

    # 9. Native GitHub APPROVED on head_sha -> approved
    approved, msg = evaluate(
        [
            {
                "id": 15,
                "commit_id": head_sha,
                "state": "APPROVED",
                "body": "",
                "submitted_at": "2026-11-28T10:40:00Z",
            }
        ],
        head_sha,
    )
    assert approved is True
    assert "Full review approved" in msg

    # 10. DISMISSED and PENDING reviews ignored -> blocked
    approved, msg = evaluate(
        [
            {
                "id": 16,
                "commit_id": head_sha,
                "state": "DISMISSED",
                "body": substantive_approval,
                "submitted_at": "2026-11-28T10:45:00Z",
            },
            {
                "id": 17,
                "commit_id": head_sha,
                "state": "PENDING",
                "body": substantive_approval,
                "submitted_at": "2026-11-28T10:46:00Z",
            },
        ],
        head_sha,
    )
    assert approved is False
    assert "Awaiting full review approval" in msg

    # 11. Neutral discussion comment after APPROVED preserves approval
    approved, msg = evaluate(
        [
            {
                "id": 18,
                "commit_id": head_sha,
                "state": "COMMENTED",
                "body": substantive_approval,
                "submitted_at": "2026-11-28T10:50:00Z",
            },
            {
                "id": 19,
                "commit_id": head_sha,
                "state": "COMMENTED",
                "body": "Thanks for adding the extra test coverage!",
                "submitted_at": "2026-11-28T10:55:00Z",
            },
        ],
        head_sha,
    )
    assert approved is True
    assert "Full review approved" in msg


def test_review_gate_cli_reviews_json(tmp_path: Path) -> None:
    head_sha = "fedcba9876543210fedcba9876543210fedcba98"
    reviews_path = tmp_path / "reviews.json"
    reviews_path.write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "commit_id": head_sha,
                    "state": "COMMENTED",
                    "body": "**Verdict:** APPROVED\n\nAll checks and security assertions verified.",
                    "submitted_at": "2026-11-28T11:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            "python3",
            str(SCRIPTS_DIR / "check-review-gate.py"),
            "--head-sha",
            head_sha,
            "--reviews-json",
            str(reviews_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Review gate [success]" in proc.stdout

    brief_path = tmp_path / "brief.json"
    brief_path.write_text(
        json.dumps(
            [
                {
                    "id": 2,
                    "commit_id": head_sha,
                    "state": "COMMENTED",
                    "body": "APPROVED",
                    "submitted_at": "2026-11-28T11:05:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    brief_proc = subprocess.run(
        [
            "python3",
            str(SCRIPTS_DIR / "check-review-gate.py"),
            "--head-sha",
            head_sha,
            "--reviews-json",
            str(brief_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert brief_proc.returncode == 1
    assert "Review gate [failure]" in brief_proc.stdout
