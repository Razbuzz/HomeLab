#!/usr/bin/env python3
"""Comprehensive live System QA Test Suite for Razbuzz/HomeLab.

Runs real-world end-to-end checks against a running HomeLab Compose stack:
  Suite 1 — Dual-Interface Reachability (127.0.0.1 AND non-loopback --host-ip)
  Suite 2 — Live Dashboard Card-by-Card Web Link Audit
  Suite 3 — Hermes Agent Full Web UI, Auth & Compose DNS Integration QA (--beta)
  Suite 4 — Per-Service Functional & Security QA (all 15 default services)

Uses Python 3 stdlib only. Never prints secret values.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SERVICES_ARRAY_RE = re.compile(r"const\s+services\s*=\s*\[(.*?)\];", re.DOTALL)
OBJECT_RE = re.compile(r"\{([^{}]+)\}")
FIELD_RE = re.compile(r'(\w+)\s*:\s*(?:"((?:[^"\\]|\\.)*)"|(null|\d+))')
ASSET_JS_RE = re.compile(r'(?:src|href)=["\'](/assets/[^"\']+\.js)["\']')
IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


@dataclass
class CheckResult:
    suite: str
    name: str
    passed: bool
    elapsed_s: float
    detail: str


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_env_file(env_path: Path) -> dict[str, str]:
    """Safely parse KEY=VALUE entries without shell evaluation or expansion."""
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            candidate = line[len("export ") :].lstrip()
            if "=" in candidate:
                key_part = candidate.split("=", 1)[0].strip()
                if KEY_RE.match(key_part):
                    line = candidate
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"')
            or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        values[key] = value
    return values


def parse_dashboard_services(html_text: str) -> list[dict[str, object]]:
    """Extract service card definitions from dashboard HTML `const services = [...]`."""
    array_match = SERVICES_ARRAY_RE.search(html_text)
    if array_match is None:
        raise ValueError("Could not locate `const services = [...]` array in dashboard HTML")

    services: list[dict[str, object]] = []
    for obj_match in OBJECT_RE.finditer(array_match.group(1)):
        fields: dict[str, object] = {}
        for field_match in FIELD_RE.finditer(obj_match.group(1)):
            key = field_match.group(1)
            str_val = field_match.group(2)
            raw_val = field_match.group(3)
            if str_val is not None:
                fields[key] = bytes(str_val, "utf-8").decode("unicode_escape")
            elif raw_val == "null":
                fields[key] = None
            else:
                fields[key] = int(raw_val)
        if "name" not in fields:
            continue
        services.append(
            {
                "name": fields.get("name"),
                "port": fields.get("port"),
                "path": fields.get("path", "") or "",
                "note": fields.get("note"),
            }
        )

    if not services:
        raise ValueError("No service entries parsed from `const services = [...]`")
    return services


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 10.0,
    follow_redirects: bool = True,
    cookie_jar: http.cookiejar.CookieJar | None = None,
) -> tuple[int, dict[str, str], bytes, str]:
    """Perform an HTTP request using urllib and return (status, lower_headers, body, final_url)."""
    handlers: list[urllib.request.BaseHandler] = []
    if not follow_redirects:
        handlers.append(_NoRedirectHandler())
    if cookie_jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar))
    opener = urllib.request.build_opener(*handlers)

    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    try:
        with opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", resp.getcode())
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            # Also retain multi-header values joined safely where needed via resp.headers.get_all
            if hasattr(resp.headers, "get_all"):
                set_cookies = resp.headers.get_all("Set-Cookie")
                if set_cookies:
                    resp_headers["set-cookie-list"] = "\n".join(set_cookies)
            body = resp.read()
            return int(status), resp_headers, body, resp.geturl()
    except urllib.error.HTTPError as err:
        resp_headers = {k.lower(): v for k, v in err.headers.items()} if err.headers else {}
        if err.headers and hasattr(err.headers, "get_all"):
            set_cookies = err.headers.get_all("Set-Cookie")
            if set_cookies:
                resp_headers["set-cookie-list"] = "\n".join(set_cookies)
        body = err.read() if err.fp else b""
        return int(err.code), resp_headers, body, err.geturl()


def http_request_with_retries(
    url: str,
    *,
    expected_statuses: tuple[int, ...] = (200,),
    attempts: int = 4,
    retry_delay: float = 1.5,
    **kwargs,
) -> tuple[int, dict[str, str], bytes, str]:
    last_exc: Exception | None = None
    last_result: tuple[int, dict[str, str], bytes, str] | None = None
    for idx in range(attempts):
        try:
            result = http_request(url, **kwargs)
            last_result = result
            if result[0] in expected_statuses:
                return result
        except Exception as exc:
            last_exc = exc
        if idx < attempts - 1:
            time.sleep(retry_delay)
    if last_result is not None:
        return last_result
    assert last_exc is not None
    raise last_exc


def run_check(results: list[CheckResult], suite: str, name: str, fn: Callable[[], str]) -> None:
    start = time.monotonic()
    try:
        detail = fn()
        results.append(CheckResult(suite, name, True, time.monotonic() - start, detail))
    except Exception as exc:
        results.append(CheckResult(suite, name, False, time.monotonic() - start, f"{type(exc).__name__}: {exc}"))


def check_dual_interface(
    results: list[CheckResult],
    *,
    loopback_ip: str,
    host_ip: str,
    dashboard_port: str,
    hermes_port: str,
    beta: bool,
) -> None:
    suite = "Suite 1: Dual-Interface Reachability"
    targets: list[tuple[str, str]] = [
        ("dashboard", f"http://{{ip}}:{dashboard_port}/"),
    ]
    if beta:
        targets.append(("hermes-agent", f"http://{{ip}}:{hermes_port}/api/status"))

    unique_ips = [loopback_ip] if host_ip == loopback_ip else [loopback_ip, host_ip]

    for svc, url_tmpl in targets:
        for ip in unique_ips:
            url = url_tmpl.format(ip=ip)

            def _probe(target_url: str = url, service: str = svc) -> str:
                status, _headers, body, _final = http_request_with_retries(
                    target_url, expected_statuses=(200,), attempts=5, retry_delay=1.5
                )
                if status != 200:
                    raise AssertionError(f"Expected HTTP 200 from {target_url}, got {status}")
                if not body.strip():
                    raise AssertionError(f"Empty response body from {target_url}")
                if service == "dashboard" and "<title>Home Server Dashboard</title>" not in body.decode("utf-8", errors="replace"):
                    raise AssertionError(f"Missing dashboard title at {target_url}")
                if service == "hermes-agent":
                    payload = json.loads(body.decode("utf-8"))
                    if payload.get("overall") != "ok":
                        raise AssertionError(f"Expected overall=='ok' from {target_url}, got {payload.get('overall')!r}")
                return f"{target_url} -> HTTP {status} ({len(body)} B)"

            run_check(results, suite, f"{svc} on {ip}", _probe)


def check_dashboard_links(
    results: list[CheckResult],
    *,
    host_ip: str,
    dashboard_port: str,
    env: dict[str, str],
    beta: bool,
) -> None:
    suite = "Suite 2: Dashboard Card-by-Card Link Audit"
    dash_url = f"http://{host_ip}:{dashboard_port}/"
    parsed_cards: list[dict[str, object]] = []

    def _fetch_and_parse_dashboard() -> str:
        status, _headers, body, _final = http_request_with_retries(dash_url, expected_statuses=(200,))
        if status != 200:
            raise AssertionError(f"Dashboard {dash_url} returned HTTP {status}")
        html = body.decode("utf-8", errors="replace")
        if "<title>Home Server Dashboard</title>" not in html:
            raise AssertionError(f"Dashboard {dash_url} missing <title>Home Server Dashboard</title>")
        cards = parse_dashboard_services(html)
        parsed_cards.extend(cards)
        return f"Fetched {dash_url} (HTTP 200), parsed {len(cards)} cards"

    run_check(results, suite, "Fetch & parse live dashboard HTML", _fetch_and_parse_dashboard)
    if not parsed_cards:
        return

    # Map card default ports to any CI-overridden ports in --env-file when applicable.
    port_overrides = {
        80: int(env.get("DASHBOARD_PORT", "80")),
        12120: int(env.get("PI_HOLE_UI_PORT", "12120")),
        5055: int(env.get("SEERR_PORT", "5055")),
        8989: int(env.get("SONARR_PORT", "8989")),
        7878: int(env.get("RADARR_PORT", "7878")),
        6246: int(env.get("MAINTAINERR_PORT", "6246")),
        9117: int(env.get("JACKETT_PORT", "9117")),
        8191: int(env.get("FLARESOLVERR_PORT", "8191")),
        8080: int(env.get("QBITTORRENT_UI_PORT", "8080")),
        10000: int(env.get("FILEBROWSER_UI_PORT", "10000")),
        5984: int(env.get("COUCHDB_PORT", "5984")),
        11434: int(env.get("OLLAMA_PORT", "11434")),
        9119: int(env.get("HERMES_DASHBOARD_PORT", "9119")),
        5800: int(env.get("QDirstat_PORT", "5800")),
    }

    for card in parsed_cards:
        name = str(card["name"])
        port = card["port"]
        path = str(card.get("path") or "")
        note = card.get("note")

        if port is None:
            run_check(results, suite, f"Card '{name}' (non-HTTP)", lambda n=name, nt=note: f"Skipped non-HTTP card (port=null, note={nt})")
            continue

        if name == "Cockpit" or note == "System":
            run_check(results, suite, f"Card '{name}' (host OS)", lambda p=port: f"Skipped host-installed system card (:{p})")
            continue

        if note == "Beta" and not beta:
            run_check(results, suite, f"Card '{name}' (beta skipped)", lambda p=port: f"Skipped beta card without --beta (:{p})")
            continue

        effective_port = port_overrides.get(int(port), int(port))
        card_url = f"http://{host_ip}:{effective_port}{path}"

        def _audit_card(target_url: str = card_url) -> str:
            card_cookies = http.cookiejar.CookieJar()
            status, headers, body, final_url = http_request_with_retries(
                target_url,
                expected_statuses=(200, 401),
                follow_redirects=False,
                cookie_jar=card_cookies,
                attempts=4,
                retry_delay=1.5,
            )
            if status in (301, 302, 303, 307, 308):
                loc = headers.get("location", "")
                if not loc:
                    raise AssertionError(f"{target_url} returned HTTP {status} without Location header")
                resolved_url = urllib.parse.urljoin(target_url, loc)
                f_status, _f_headers, f_body, f_final = http_request_with_retries(
                    resolved_url,
                    expected_statuses=(200, 401),
                    follow_redirects=True,
                    cookie_jar=card_cookies,
                    attempts=4,
                    retry_delay=1.5,
                )
                if f_status not in (200, 401):
                    raise AssertionError(f"{target_url} -> {resolved_url} returned HTTP {f_status}")
                if f_status == 200 and not f_body.strip():
                    raise AssertionError(f"{target_url} -> {f_final} returned empty body")
                return f"{target_url} -> HTTP {status} -> {f_final} HTTP {f_status} ({len(f_body)} B)"

            if status not in (200, 401):
                raise AssertionError(f"{target_url} returned HTTP {status}")
            if status == 200 and not body.strip():
                raise AssertionError(f"{target_url} returned empty response body")
            return f"{final_url} -> HTTP {status} ({len(body)} B)"

        run_check(results, suite, f"Card '{name}' ({card_url})", _audit_card)


def check_hermes_qa(
    results: list[CheckResult],
    *,
    host_ip: str,
    hermes_port: str,
    username: str,
    password: str,
) -> None:
    suite = "Suite 3: Hermes Agent Web UI, Auth & Integration QA"
    base_url = f"http://{host_ip}:{hermes_port}"
    cookie_jar = http.cookiejar.CookieJar()
    spa_html_holder: dict[str, str] = {}

    def _step1_status() -> str:
        url = f"{base_url}/api/status"
        status, _h, body, _ = http_request_with_retries(url, expected_statuses=(200,))
        if status != 200:
            raise AssertionError(f"GET {url} returned HTTP {status}")
        data = json.loads(body.decode("utf-8"))
        if data.get("overall") != "ok":
            raise AssertionError(f"Expected overall == 'ok', got {data.get('overall')!r}")
        if data.get("gateway_running") is not True:
            raise AssertionError(f"Expected gateway_running is True, got {data.get('gateway_running')!r}")
        if data.get("auth_required") is not True:
            raise AssertionError(f"Expected auth_required is True, got {data.get('auth_required')!r}")
        providers = data.get("auth_providers") or []
        if "basic" not in providers:
            raise AssertionError(f"Expected 'basic' in auth_providers, got {providers!r}")
        return f"GET /api/status -> HTTP 200 (overall=ok, gateway_running=True, auth_providers={providers})"

    def _step2_unauth_root() -> str:
        url = f"{base_url}/"
        status, headers, _body, _ = http_request(url, follow_redirects=False)
        if status != 302:
            raise AssertionError(f"Expected unauthenticated GET / -> 302, got {status}")
        location = headers.get("location", "")
        if "/login" not in location:
            raise AssertionError(f"Expected Location containing '/login', got {location!r}")
        return f"Unauth GET / -> HTTP 302 Location={location}"

    def _step3_unauth_sessions() -> str:
        url = f"{base_url}/api/sessions"
        status, _h, _b, _ = http_request(url, follow_redirects=False)
        if status != 401:
            raise AssertionError(f"Expected unauthenticated GET /api/sessions -> 401, got {status}")
        return "Unauth GET /api/sessions -> HTTP 401 (fail-closed)"

    def _step4_login_page() -> str:
        url = f"{base_url}/login"
        status, _h, body, _ = http_request(url)
        if status != 200:
            raise AssertionError(f"GET {url} returned HTTP {status}")
        text = body.decode("utf-8", errors="replace")
        lower = text.lower()
        if "<!doctype html>" not in lower and "<html" not in lower:
            raise AssertionError("Login page missing HTML document markers")
        if "/auth/password-login" not in text and "password" not in lower:
            raise AssertionError("Login page missing password login form markers")
        return f"GET /login -> HTTP 200 HTML ({len(body)} B)"

    def _step5_invalid_login() -> str:
        url = f"{base_url}/auth/password-login"
        payload = json.dumps(
            {
                "provider": "basic",
                "username": username,
                "password": "wrong-password-xyz",
                "next": "/",
            }
        ).encode("utf-8")
        status, _h, _b, _ = http_request(
            url,
            method="POST",
            headers={"Content-Type": "application/json", "Origin": base_url},
            data=payload,
            follow_redirects=False,
        )
        if status != 401:
            raise AssertionError(f"Expected invalid password login -> HTTP 401, got {status}")
        return "Invalid password POST /auth/password-login -> HTTP 401"

    def _step6_valid_login() -> str:
        url = f"{base_url}/auth/password-login"
        payload = json.dumps(
            {
                "provider": "basic",
                "username": username,
                "password": password,
                "next": "/",
            }
        ).encode("utf-8")
        status, headers, body, _ = http_request(
            url,
            method="POST",
            headers={"Content-Type": "application/json", "Origin": base_url},
            data=payload,
            follow_redirects=False,
            cookie_jar=cookie_jar,
        )
        if status != 200:
            raise AssertionError(f"Valid login POST {url} returned HTTP {status}")
        data = json.loads(body.decode("utf-8"))
        if data.get("ok") is not True:
            raise AssertionError(f"Expected JSON {{'ok': true}}, got {data!r}")

        raw_set_cookies = headers.get("set-cookie-list") or headers.get("set-cookie", "")
        session_at_lines = [
            line.strip()
            for line in raw_set_cookies.splitlines()
            if "hermes_session_at=" in line
        ]
        if not session_at_lines:
            raise AssertionError(f"Missing Set-Cookie for hermes_session_at (got headers: {raw_set_cookies!r})")
        for cookie_hdr in session_at_lines:
            attrs = [part.strip().lower() for part in cookie_hdr.split(";")[1:]]
            if "secure" in attrs:
                raise AssertionError(f"hermes_session_at cookie unexpectedly marked Secure over plain HTTP: {cookie_hdr!r}")
        return "Valid POST /auth/password-login -> HTTP 200 ok=True, hermes_session_at cookie set without Secure"

    def _step7_auth_spa_and_asset() -> str:
        url = f"{base_url}/"
        status, _h, body, _ = http_request(url, follow_redirects=False, cookie_jar=cookie_jar)
        if status != 200:
            raise AssertionError(f"Authenticated GET / returned HTTP {status}")
        html = body.decode("utf-8", errors="replace")
        if "<title>Hermes Agent - Dashboard</title>" not in html:
            raise AssertionError("Authenticated SPA HTML missing '<title>Hermes Agent - Dashboard</title>'")
        spa_html_holder["html"] = html

        match = ASSET_JS_RE.search(html)
        if match is None:
            raise AssertionError("Could not find any '/assets/*.js' reference in authenticated SPA HTML")
        asset_path = match.group(1)
        asset_url = f"{base_url}{asset_path}"
        a_status, _ah, a_body, _ = http_request(asset_url, follow_redirects=False, cookie_jar=cookie_jar)
        if a_status != 200:
            raise AssertionError(f"GET {asset_url} returned HTTP {a_status}")
        if len(a_body) <= 1024:
            raise AssertionError(f"Asset {asset_url} unexpectedly small ({len(a_body)} B <= 1024 B)")
        return f"Auth GET / -> HTTP 200 SPA + {asset_path} -> HTTP 200 ({len(a_body)} B)"

    def _step8_auth_sessions() -> str:
        url = f"{base_url}/api/sessions"
        status, _h, body, _ = http_request(url, follow_redirects=False, cookie_jar=cookie_jar)
        if status != 200:
            raise AssertionError(f"Authenticated GET {url} returned HTTP {status}")
        data = json.loads(body.decode("utf-8"))
        if "sessions" not in data:
            raise AssertionError(f"Expected 'sessions' key in /api/sessions response, got keys {list(data.keys())!r}")
        return f"Auth GET /api/sessions -> HTTP 200 (sessions={len(data['sessions'])})"

    def _step9_cross_container_dns() -> str:
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "hermes-agent",
                "python3",
                "-c",
                "import urllib.request; print(urllib.request.urlopen('http://ollama:11434/api/version', timeout=5).read().decode())",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(f"docker exec failed (rc={proc.returncode}): {proc.stderr.strip()}")
        payload = json.loads(proc.stdout.strip())
        if "version" not in payload:
            raise AssertionError(f"Expected 'version' in Ollama response from hermes-agent, got {payload!r}")
        return f"hermes-agent -> http://ollama:11434/api/version -> version={payload['version']}"

    run_check(results, suite, "1. GET /api/status (public probe)", _step1_status)
    run_check(results, suite, "2. Unauthenticated GET / -> 302 /login", _step2_unauth_root)
    run_check(results, suite, "3. Unauthenticated GET /api/sessions -> 401", _step3_unauth_sessions)
    run_check(results, suite, "4. GET /login HTML page", _step4_login_page)
    run_check(results, suite, "5. Invalid password POST /auth/password-login -> 401", _step5_invalid_login)
    run_check(results, suite, "6. Valid login POST /auth/password-login + cookie", _step6_valid_login)
    run_check(results, suite, "7. Authenticated SPA HTML & JS bundle", _step7_auth_spa_and_asset)
    run_check(results, suite, "8. Authenticated GET /api/sessions", _step8_auth_sessions)
    run_check(results, suite, "9. Cross-container Compose DNS (hermes-agent -> ollama)", _step9_cross_container_dns)


def check_per_service_functional(
    results: list[CheckResult],
    *,
    host_ip: str,
    env: dict[str, str],
) -> None:
    suite = "Suite 4: Per-Service Functional & Security QA"

    def _check_json_endpoint(url: str, check_fn: Callable[[dict], str]) -> str:
        status, _h, body, _ = http_request_with_retries(url, expected_statuses=(200,))
        if status != 200:
            raise AssertionError(f"{url} returned HTTP {status}")
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise AssertionError(f"Expected JSON object from {url}, got {type(data).__name__}")
        return check_fn(data)

    def _plex() -> str:
        url = f"http://{host_ip}:32400/identity"
        status, _h, body, _ = http_request_with_retries(url, expected_statuses=(200,))
        if status != 200:
            raise AssertionError(f"{url} returned HTTP {status}")
        root = ET.fromstring(body.decode("utf-8"))
        if root.tag != "MediaContainer":
            raise AssertionError(f"Expected XML root 'MediaContainer', got {root.tag!r}")
        return f"{url} -> HTTP 200 XML root={root.tag}"

    def _qbittorrent() -> str:
        port = env.get("QBITTORRENT_UI_PORT", "8080")
        url = f"http://{host_ip}:{port}/"
        status, _h, body, _ = http_request_with_retries(url, expected_statuses=(200,))
        if status != 200:
            raise AssertionError(f"{url} returned HTTP {status}")
        html = body.decode("utf-8", errors="replace").strip()
        if not html or "<html" not in html.lower():
            raise AssertionError(f"{url} did not return non-empty HTML")
        return f"{url} -> HTTP 200 HTML ({len(body)} B)"

    def _filebrowser() -> str:
        port = env.get("FILEBROWSER_UI_PORT", "10000")
        url = f"http://{host_ip}:{port}/health"
        return _check_json_endpoint(
            url,
            lambda d: (
                f"{url} -> HTTP 200 status={d.get('status')}"
                if d.get("status") == "OK"
                else (_ for _ in ()).throw(AssertionError(f"Expected status=='OK', got {d!r}"))
            ),
        )

    def _samba() -> str:
        port = int(env.get("SAMBA_PORT", "445"))
        share_name = env.get("SAMBA_SHARE_NAME", "Data")
        with socket.create_connection((host_ip, port), timeout=5):
            pass
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "samba",
                "smbclient",
                "--configfile=/etc/samba.conf",
                "-L",
                "//127.0.0.1",
                "-U",
                "%",
                "-m",
                "SMB3",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(f"smbclient failed (rc={proc.returncode}): {proc.stderr.strip()}")
        if share_name not in proc.stdout:
            raise AssertionError(f"Configured share {share_name!r} missing from smbclient output:\n{proc.stdout}")
        return f"TCP {host_ip}:{port} + smbclient lists share '{share_name}'"

    def _sonarr() -> str:
        port = env.get("SONARR_PORT", "8989")
        url = f"http://{host_ip}:{port}/ping"
        return _check_json_endpoint(
            url,
            lambda d: (
                f"{url} -> HTTP 200 status={d.get('status')}"
                if d.get("status") == "OK"
                else (_ for _ in ()).throw(AssertionError(f"Expected status=='OK', got {d!r}"))
            ),
        )

    def _radarr() -> str:
        port = env.get("RADARR_PORT", "7878")
        url = f"http://{host_ip}:{port}/ping"
        return _check_json_endpoint(
            url,
            lambda d: (
                f"{url} -> HTTP 200 status={d.get('status')}"
                if d.get("status") == "OK"
                else (_ for _ in ()).throw(AssertionError(f"Expected status=='OK', got {d!r}"))
            ),
        )

    def _jackett() -> str:
        port = env.get("JACKETT_PORT", "9117")
        url = f"http://{host_ip}:{port}/health"
        return _check_json_endpoint(
            url,
            lambda d: (
                f"{url} -> HTTP 200 status={d.get('status')}"
                if d.get("status") == "OK"
                else (_ for _ in ()).throw(AssertionError(f"Expected status=='OK', got {d!r}"))
            ),
        )

    def _maintainerr() -> str:
        port = env.get("MAINTAINERR_PORT", "6246")
        url = f"http://{host_ip}:{port}/api/health/ready"

        def _validate(d: dict) -> str:
            if d.get("status") != "ok":
                raise AssertionError(f"Expected status=='ok', got {d!r}")
            db = d.get("database")
            db_ok = db == "ok" or (isinstance(db, dict) and db.get("status") == "ok")
            if not db_ok:
                raise AssertionError(f"Expected database=='ok', got {d!r}")
            return f"{url} -> HTTP 200 status=ok database={db!r}"

        return _check_json_endpoint(url, _validate)

    def _couchdb() -> str:
        port = env.get("COUCHDB_PORT", "5984")
        up_url = f"http://{host_ip}:{port}/_up"
        up_msg = _check_json_endpoint(
            up_url,
            lambda d: (
                f"{up_url} status=ok"
                if d.get("status") == "ok"
                else (_ for _ in ()).throw(AssertionError(f"Expected status=='ok', got {d!r}"))
            ),
        )
        all_dbs_url = f"http://{host_ip}:{port}/_all_dbs"
        status, _h, _b, _ = http_request(all_dbs_url, follow_redirects=False)
        if status != 401:
            raise AssertionError(f"Expected unauthenticated {all_dbs_url} -> 401, got {status}")
        return f"{up_msg} + unauth /_all_dbs -> HTTP 401"

    def _pihole() -> str:
        port = env.get("PI_HOLE_UI_PORT", "12120")
        login_url = f"http://{host_ip}:{port}/api/info/login"
        status, _h, body, _ = http_request_with_retries(login_url, expected_statuses=(200,))
        if status != 200:
            raise AssertionError(f"{login_url} returned HTTP {status}")
        json.loads(body.decode("utf-8"))

        proc = subprocess.run(
            [
                "docker",
                "exec",
                "pihole",
                "dig",
                "+short",
                "+norecurse",
                "+retry=0",
                "@127.0.0.1",
                "pi.hole",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(f"in-container dig failed (rc={proc.returncode}): {proc.stderr.strip()}")
        answers = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not answers or not any(IPV4_RE.match(ans) for ans in answers):
            raise AssertionError(f"Unexpected pi.hole in-container DNS answer: {proc.stdout!r}")

        host_dig_msg = "host dig unavailable"
        if shutil_which("dig") is not None:
            dns_port = env.get("PI_HOLE_CI_DNS_PORT", "15353")
            # Pi-hole v6 defaults dns.listeningMode="LOCAL", which accepts loopback/same-subnet
            # traffic and drops spoofed non-local-bridge host IPs unless FTLCONF_dns_listeningMode=ALL.
            # Test external host-port DNS reachability via loopback (and host_ip when accepted).
            probe_ips = ["127.0.0.1"] if host_ip == "127.0.0.1" else [host_ip, "127.0.0.1"]
            host_answer = None
            host_errors: list[str] = []
            for probe_ip in probe_ips:
                h_proc = subprocess.run(
                    [
                        "dig",
                        "+short",
                        "+time=3",
                        "+tries=1",
                        f"@{probe_ip}",
                        "-p",
                        dns_port,
                        "pi.hole",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if h_proc.returncode == 0:
                    h_answers = [line.strip() for line in h_proc.stdout.splitlines() if line.strip()]
                    if h_answers and any(IPV4_RE.match(ans) for ans in h_answers):
                        host_answer = f"{probe_ip}:{dns_port}={h_answers[0]}"
                        break
                    host_errors.append(f"@{probe_ip}:{dns_port} empty output: {h_proc.stdout!r}")
                else:
                    host_errors.append(f"@{probe_ip}:{dns_port} rc={h_proc.returncode}")
            if host_answer is None:
                raise AssertionError("Host dig failed across all interfaces: " + "; ".join(host_errors))
            host_dig_msg = f"host dig @{host_answer}"

        return f"{login_url} HTTP 200 + container dig={answers[0]} + {host_dig_msg}"

    def _seerr() -> str:
        port = env.get("SEERR_PORT", "5055")
        url = f"http://{host_ip}:{port}/api/v1/settings/public"
        status, _h, body, _ = http_request_with_retries(url, expected_statuses=(200,))
        if status != 200:
            raise AssertionError(f"{url} returned HTTP {status}")
        json.loads(body.decode("utf-8"))
        proc = subprocess.run(
            ["docker", "exec", "seerr", "printenv", "TZ"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(f"docker exec seerr printenv TZ failed: {proc.stderr.strip()}")
        tz_val = proc.stdout.strip()
        expected_tz = env.get("DEFAULT_TIMEZONE", "UTC")
        if tz_val != expected_tz:
            raise AssertionError(f"Expected seerr TZ={expected_tz!r}, got {tz_val!r}")
        return f"{url} HTTP 200 + container TZ={tz_val}"

    def _flaresolverr() -> str:
        port = env.get("FLARESOLVERR_PORT", "8191")
        url = f"http://{host_ip}:{port}/health"
        return _check_json_endpoint(
            url,
            lambda d: (
                f"{url} -> HTTP 200 status={d.get('status')}"
                if d.get("status") == "ok"
                else (_ for _ in ()).throw(AssertionError(f"Expected status=='ok', got {d!r}"))
            ),
        )

    def _ollama() -> str:
        port = env.get("OLLAMA_PORT", "11434")
        url = f"http://{host_ip}:{port}/api/version"
        ver_msg = _check_json_endpoint(
            url,
            lambda d: (
                f"{url} version={d['version']}"
                if "version" in d
                else (_ for _ in ()).throw(AssertionError(f"Expected 'version' key, got {d!r}"))
            ),
        )
        proc = subprocess.run(
            ["docker", "exec", "ollama", "printenv", "OLLAMA_CONTEXT_LENGTH"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(f"docker exec ollama printenv OLLAMA_CONTEXT_LENGTH failed: {proc.stderr.strip()}")
        ctx_val = proc.stdout.strip()
        expected_ctx = env.get("OLLAMA_CONTEXT_LENGTH", "65536")
        if ctx_val != expected_ctx:
            raise AssertionError(f"Expected OLLAMA_CONTEXT_LENGTH={expected_ctx!r}, got {ctx_val!r}")
        return f"{ver_msg} + OLLAMA_CONTEXT_LENGTH={ctx_val}"

    def _qdirstat() -> str:
        port = env.get("QDirstat_PORT", "5800")
        url = f"http://{host_ip}:{port}/site.webmanifest"
        return _check_json_endpoint(url, lambda _d: f"{url} -> HTTP 200 valid JSON")

    run_check(results, suite, "plex (/identity XML)", _plex)
    run_check(results, suite, "qbittorrent (Web UI HTML)", _qbittorrent)
    run_check(results, suite, "filebrowser (/health JSON)", _filebrowser)
    run_check(results, suite, "samba (TCP + smbclient share list)", _samba)
    run_check(results, suite, "sonarr (/ping JSON)", _sonarr)
    run_check(results, suite, "radarr (/ping JSON)", _radarr)
    run_check(results, suite, "jackett (/health JSON)", _jackett)
    run_check(results, suite, "maintainerr (/api/health/ready JSON)", _maintainerr)
    run_check(results, suite, "couchdb (/_up + /_all_dbs 401)", _couchdb)
    run_check(results, suite, "pihole (/api/info/login + DNS dig)", _pihole)
    run_check(results, suite, "seerr (public settings + TZ)", _seerr)
    run_check(results, suite, "flaresolverr (/health JSON)", _flaresolverr)
    run_check(results, suite, "ollama (/api/version + OLLAMA_CONTEXT_LENGTH)", _ollama)
    run_check(results, suite, "qdirstat (/site.webmanifest JSON)", _qdirstat)


def shutil_which(binary: str) -> str | None:
    import shutil

    return shutil.which(binary)


def print_report(results: list[CheckResult], total_elapsed_s: float) -> None:
    print("")
    print("====================================================================")
    print(" HomeLab System QA Report")
    print("====================================================================")

    current_suite = None
    passed_count = 0
    failed_count = 0

    for res in results:
        if res.suite != current_suite:
            current_suite = res.suite
            print(f"\n[{current_suite}]")
        status_tag = "PASS" if res.passed else "FAIL"
        if res.passed:
            passed_count += 1
        else:
            failed_count += 1
        print(f"  [{status_tag}] {res.name:<48} ({res.elapsed_s:.2f}s) - {res.detail}")

    print("\n--------------------------------------------------------------------")
    print(
        f"Summary: {passed_count} passed, {failed_count} failed, "
        f"{len(results)} total checks in {total_elapsed_s:.2f}s"
    )
    print("--------------------------------------------------------------------")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run comprehensive System QA assertions against a live HomeLab stack."
    )
    parser.add_argument(
        "--env-file",
        required=True,
        type=Path,
        help="Path to active .env file containing effective ports and test credentials.",
    )
    parser.add_argument(
        "--host-ip",
        required=True,
        help="Non-loopback host IP address (or fallback IP) to test LAN/remote access.",
    )
    parser.add_argument(
        "--loopback",
        default="127.0.0.1",
        help="Loopback IP address for dual-interface checks (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--beta",
        action="store_true",
        help="Include beta profile checks (Hermes Agent Web UI, auth flow, and DNS integration).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.env_file.is_file():
        print(f"Error: --env-file does not exist: {args.env_file}", file=sys.stderr)
        return 1

    env = parse_env_file(args.env_file)
    results: list[CheckResult] = []
    overall_start = time.monotonic()

    dashboard_port = env.get("DASHBOARD_PORT", "80")
    hermes_port = env.get("HERMES_DASHBOARD_PORT", "9119")

    check_dual_interface(
        results,
        loopback_ip=args.loopback,
        host_ip=args.host_ip,
        dashboard_port=dashboard_port,
        hermes_port=hermes_port,
        beta=args.beta,
    )

    check_dashboard_links(
        results,
        host_ip=args.host_ip,
        dashboard_port=dashboard_port,
        env=env,
        beta=args.beta,
    )

    if args.beta:
        check_hermes_qa(
            results,
            host_ip=args.host_ip,
            hermes_port=hermes_port,
            username=env.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "admin"),
            password=env.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", ""),
        )

    check_per_service_functional(
        results,
        host_ip=args.host_ip,
        env=env,
    )

    print_report(results, time.monotonic() - overall_start)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
