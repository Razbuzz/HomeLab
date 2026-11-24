# HomeLab Backlog & Roadmap

This document tracks planned features, operational tooling, and architectural improvements for **Razbuzz/HomeLab**. Each item captures problem context, dependencies, proposed architecture, security considerations, and concrete acceptance criteria so future contributors and AI agents can pick up tasks cleanly.

> **Note:** Items marked **Planned** are not implemented yet. Referenced script paths, systemd units, configuration files, and Makefile targets inside individual proposals describe the target design rather than existing files.

## Status Summary

| ID | Task | Priority | Status | Target Branch | Dependencies |
| :--- | :--- | :--- | :--- | :--- | :--- |
| [BL-001](#bl-001-automatic-release-updater) | Automatic Release Updater | High | Planned | `feat/auto-update` | Safe upgrade & rollback workflow (`scripts/backup.sh`, `scripts/upgrade.sh`, `scripts/rollback.sh`) |
| [BL-002](#bl-002-user-friendly-untracked-configuration-architecture) | User-Friendly, Untracked Configuration Architecture | High | Planned | `feat/config-file-migration` | None (foundational DX & upgrade safety improvement) |

---

## BL-001: Automatic Release Updater

- **Branch:** `feat/auto-update`
- **Priority:** High
- **Status:** Planned
- **Dependencies:** Safe backup, upgrade, health verification, and automatic rollback workflow (`scripts/backup.sh`, `scripts/upgrade.sh`, `scripts/rollback.sh`)

### Problem / Goal

Keep HomeLab hosts automatically updated when a new GitHub Release (`v*.*.*`) is published on `Razbuzz/HomeLab`, without risking persistent data loss, silent service outages, or repeated crash-loop upgrades.

### Proposed Architecture

- **Opt-in Systemd Timer & Service:**
  - Provide `homelab-autoupdate.timer` and `homelab-autoupdate.service` (installed opt-in via `make autoupdate-enable` / `make autoupdate-disable` or a setup script).
  - Configurable maintenance window and cadence (default: weekly on Sunday at `03:00` local time with randomized delay/jitter).
- **Release Discovery & Semver Policy:**
  - Poll the GitHub Releases API (`repos/Razbuzz/HomeLab/releases/latest` or `releases` list) to detect published release tags (`v*.*.*`).
  - Ignore prereleases (`-beta`, `-rc`, `-alpha`, etc.) unless explicitly opted in.
  - Compare semantic versions (`MAJOR.MINOR.PATCH`) against the host's currently installed version/tag.
  - Policy controls via allowlist / bump-level setting:
    - **Default:** automatically apply **patch** (`vX.Y.Z+1`) and **minor** (`vX.Y+1.0`) releases.
    - **Major releases (`vX+1.0.0`):** hold for explicit manual approval (and optionally notify the operator) unless auto-major upgrades are explicitly enabled.
- **Reliability, Locking & Circuit-Breaker:**
  - Use an exclusive `flock` lockfile (e.g., `/run/lock/homelab-autoupdate.lock` or repo-local runtime lock) so concurrent timer ticks or manual `make` operations never overlap.
  - Persist state (last attempted tag, status, timestamp) in a local state file acting as a **circuit-breaker**: if a given release tag previously failed or triggered a rollback, skip it on subsequent timer ticks until the operator intervenes or a newer release is published.
  - Delegate the actual update execution to `scripts/upgrade.sh --target <tag>`, which:
    1. Creates and verifies a pre-upgrade backup (`scripts/backup.sh`).
    2. Checks out / updates to `<tag>`, runs migrations/permissions, and brings up the stack.
    3. Verifies healthchecks across all active services (including `beta` profile services only if enabled on the host).
    4. Automatically invokes `scripts/rollback.sh` on any validation or healthcheck failure.
- **Optional Notifications:**
  - Support optional outbound notifications (generic JSON webhook, ntfy, Discord, or Telegram) reporting:
    - Successful upgrade (`old_tag` -> `new_tag`).
    - Held major release awaiting manual review.
    - Failed upgrade with automatic rollback status.

### Security Considerations

- **Release & Tag Integrity:** Verify git tags/commits (annotated/signed tag verification or GitHub release-to-commit SHA validation) before checking out or executing code from a new release.
- **Least-Privilege Execution:** Run unprivileged discovery/checks where possible and scope elevated privileges strictly to Docker/systemd actions required for stack restart; avoid `curl | bash` patterns or unvalidated remote scripts.
- **Secret Hygiene:** Never log, echo, or include `.env` secrets, API tokens, or webhook URLs/auth headers in journald output or notification payloads.
- **Fail-Closed Defaults:** Auto-update must remain disabled by default until explicitly enabled by the host administrator, and major releases must require opt-in.

### Acceptance Criteria

- [ ] Opt-in systemd units (`homelab-autoupdate.timer` and `homelab-autoupdate.service`) and install/enable/disable helpers exist and pass `shellcheck` / unit validation.
- [ ] Updater polls GitHub Releases (`Razbuzz/HomeLab`), skips prereleases by default, and correctly parses and compares semver (`vMAJOR.MINOR.PATCH`).
- [ ] Semver policy applies patch and minor releases automatically by default while holding major (`vX.0.0`) releases unless explicitly opted in.
- [ ] `flock` single-instance locking prevents overlapping runs, and the failure state file (circuit-breaker) prevents retrying a failed tag on future timer ticks.
- [ ] Upgrades execute exclusively through `scripts/upgrade.sh --target <tag>` (with verified backup via `scripts/backup.sh`, full healthcheck verification, and automatic rollback via `scripts/rollback.sh` on failure).
- [ ] Optional notifications (webhook / ntfy / Discord / Telegram) report success, skipped major releases, and rollback alerts without leaking secrets.
- [ ] Release integrity checks (tag/commit verification) are implemented and covered by unit/integration tests (mocked GitHub API + mocked upgrade/rollback failure cases).
- [ ] `README.md`, `AGENTS.md`, and CI tests (`make check`) are updated and passing.

---

## BL-002: User-Friendly, Untracked Configuration Architecture

- **Branch:** `feat/config-file-migration`
- **Priority:** High
- **Status:** Planned
- **Dependencies:** None

### Problem / Goal

Today `.env` is tracked in git as a template with `CHANGE_TO_*` placeholders while simultaneously being edited in place by operators on their HomeLab host. This creates several recurring pain points:

- **Update conflicts:** Running `git pull` or checking out a new release tag collides with local host edits in `.env`.
- **Accidental secret exposure or data loss:** Running `git checkout .env` or `git stash`/`git reset` can wipe live passwords and mount paths, or operators risk committing real credentials/LAN IPs.
- **Developer-centric flat UX:** A ~100-line flat `KEY=VALUE` file lacks logical grouping, inline guidance, type validation, or interactive prompting when new variables are introduced across releases.

The goal is to separate git-tracked templates from untracked host configuration, add a structured and validated configuration source, provide an interactive setup/upgrade wizard, and support seamless one-command migration from legacy `.env` files.

### Proposed Architecture

- **Decouple Tracked Template from Untracked Host Config:**
  - Stop tracking `.env` in git (`git rm --cached .env`) while keeping `.env` in `.gitignore` so live host secrets and rendered env vars are never tracked or overwritten by `git pull` / tag checkouts.
  - Ship tracked template artifacts in git instead: e.g. `.env.example` and/or `homelab.yaml.example` + a formal schema (`configs/homelab.schema.json`), keeping user files (`homelab.yaml`, `secrets.env`, rendered `.env`) untracked with restrictive host permissions (`chmod 600`).
- **Structured Configuration + Deterministic Rendering (`scripts/render-config.py`):**
  - Introduce a human-friendly structured configuration file (`homelab.yaml`) organized into clear domains (`host`, `storage`, `media`, `network`, `ai`, `secrets` — or separate non-secret `homelab.yaml` + `secrets.env` so secrets stay isolated).
  - Implement a zero-dependency Python 3.12 stdlib script (`scripts/render-config.py`, or stdlib-compatible parser if YAML/JSON subset or TOML is chosen — note Python 3.12 includes `tomllib` in stdlib whereas YAML requires careful scoping or `PyYAML`) that deterministically renders `.env` for Docker Compose.
  - Enforce strict pre-render validation:
    - Valid IPv4/IPv6 or loopback address for `DEFAULT_LAN_IP`.
    - Existing, non-symlink directory check for `DEFAULT_STORAGE_MOUNT`.
    - Valid IANA timezone (`zoneinfo.ZoneInfo` in Python 3.12 stdlib) for `DEFAULT_TIMEZONE`.
    - Numeric non-negative `DEFAULT_PUID` / `DEFAULT_GUID`.
    - Unique host ports with collision detection across all enabled services.
    - Rejection of unreplaced `CHANGE_TO_*` placeholders before starting the stack.
- **Interactive Guided Setup & Upgrade Wizard (`make configure`):**
  - Provide `make configure` for both first-time host setup and post-upgrade configuration of newly added options.
  - Displays sensible defaults, groups prompts by domain/service, masks secret input (`getpass`), supports non-interactive flags/CI mode, and validates every entry immediately.
- **Seamless Legacy Migration (`scripts/migrate-config.py`):**
  - Provide a one-command, idempotent migration script (`scripts/migrate-config.py`) that imports an existing `.env` file into the new untracked config layout.
  - Automatically creates a timestamped, `chmod 600` backup of the existing `.env` before writing new files, preserves all custom ports/credentials/paths with zero data loss, and verifies rendered output matches the original key/value set.

### Security Considerations

- **File Permissions:** Enforce `chmod 600` (and owner-only access) on `homelab.yaml`, `secrets.env`, `.env`, and migration backups.
- **Git Hygiene:** Ensure `.gitignore` ignores all untracked user config, secret files, and backup artifacts; add CI checks preventing real secrets or tracked `.env` regressions.
- **No Shell Execution During Parsing:** Render and parse configuration purely as data (never `source` user-edited files in bash) so special characters (`$`, `&`, `;`, quotes) in passwords remain safe.

### Acceptance Criteria

- [ ] `.env` is untracked in git (`git ls-files .env` is empty) and ignored via `.gitignore`, eliminating `git pull` / release checkout merge conflicts on user hosts.
- [ ] Git-tracked template/schema files (`.env.example` / `homelab.yaml.example` / schema) are added and kept in parity with `docker-compose.yml` and `README.md`.
- [ ] `scripts/render-config.py` deterministically generates `.env` (`chmod 600`) and fails fast on invalid IPs, missing storage paths, invalid IANA timezones, non-numeric UID/GID, port collisions, or `CHANGE_TO_*` placeholders.
- [ ] `make configure` runs an interactive guided wizard (with masked secret prompts, grouped sections, defaults, and immediate validation) and supports upgrading existing configs when new variables are added.
- [ ] `scripts/migrate-config.py` imports legacy `.env` files idempotently with a `chmod 600` backup and zero data loss, verified by unit tests.
- [ ] CI parity tests (`tests/test_homelab.py`), `Makefile` targets (`validate`, `system-test`, `check`), `scripts/`, `README.md`, and `AGENTS.md` are updated and `make check` passes cleanly.
