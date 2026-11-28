# HomeLab Agent Guide

Universal entrypoint for AI coding agents and assistants working on **Razbuzz/HomeLab**.

## Project Overview

HomeLab is a Docker Compose infrastructure-as-code stack designed for Ubuntu Server (with optional NVIDIA GPU acceleration for Plex hardware transcoding and Ollama local LLM inference). It provides an automated media acquisition pipeline, network DNS/ad-blocking, self-hosted collaboration/storage tools, and a custom web dashboard.

Key components:

- `docker-compose.yml` — Service definitions, volume mounts, ports, GPU reservations, and restart policies.
- `.env` — Environment configuration template with `CHANGE_TO_*` placeholders for secrets and host-specific values.
- `Makefile` — Stack lifecycle (`up`, `down`, `restart`, `logs`, `pull`, `clean`, `setup`), safe ops (`backup`, `upgrade`, `rollback`), and quality checks (`lint`, `validate`, `test`, `system-test`, `qa`, `check`), plus `changelog` when release tooling is present.
- `scripts/` — Host bootstrap, apt dependency installation, systemd service setup, and safe backup/upgrade/rollback scripts (`backup.sh`, `upgrade.sh`, `rollback.sh`, `merge-env.py`).
- `docs/` — Operator guides including [`docs/upgrading.md`](docs/upgrading.md), [`docs/hermes-agent.md`](docs/hermes-agent.md), and [`docs/vllm.md`](docs/vllm.md).
- `apps/home-server-dashboard/` — Custom Nginx static dashboard (`html/index.html`) listing web services and ports.
- `tests/` — Automated pytest suite validating compose rendering, `.env` ↔ `README.md` parity, port uniqueness, dashboard port consistency, script safety, and upgrade/rollback workflows.

## Repository Layout

```text
.
├── AGENTS.md                         # Universal agent entrypoint (this file)
├── .agents/                          # Reusable role definitions and playbooks
│   ├── README.md                     # Agent directory structure, invocation & model tiers
│   └── agents/
│       ├── developer.md              # Feature & bugfix implementation playbook
│       ├── maintainer.md             # Triage, merging, releases & dependency hygiene
│       ├── researcher.md             # Service evaluation & architecture specs
│       └── reviewer.md               # Read-only PR, infra & security review
├── .github/workflows/                # CI quality gate (ci.yml) & release automation (release.yml)
├── apps/
│   └── home-server-dashboard/        # Nginx dashboard Dockerfile and html/index.html
├── docs/
│   ├── BACKLOG.md                    # Planned roadmap items & architecture specs
│   ├── hermes-agent.md               # Hermes Agent (beta) setup & hardening guide
│   ├── upgrading.md                  # Safe upgrade, backup, and rollback guide
│   └── vllm.md                       # vLLM (opt-in GPU inference) setup & VRAM guide
├── scripts/                          # Host setup, systemd unit, backup/upgrade/rollback, and vllm-model.sh
├── tests/                            # Pytest validation suite
├── .env                              # Environment defaults and secret placeholders
├── Makefile                          # Operator and CI commands
├── README.md                         # User setup, .env mirror, and service docs
└── docker-compose.yml                # Main Compose stack
```

## Non-Negotiable Conventions

### 1. Workspace Isolation
- **Never** edit files or switch branches in the main shared clone if working concurrently; always use an isolated git worktree branched from `origin/develop`.

### 2. Branching & Release Model
- Branch from `develop` using prefixes: `feat/<kebab>`, `fix/<kebab>`, `ci/<kebab>`, `docs/<kebab>`, or `chore/<kebab>`.
- Open pull requests targeting `develop`. Feature/fix/ci/docs/chore PRs targeting `develop` MUST be **squash-merged** (`gh pr merge <n> --squash --delete-branch`, enforced by GitHub ruleset `develop-squash-only` and `delete_branch_on_merge`), automatically pruning the merged branch.
- **Hard rule: NO branch is ever merged without a full review (`Review gate` passing on the current `HEAD` commit) AND a full CI pipeline pass (`CI gate` passing).**
- Cut releases via a `develop` → `main` pull request using a merge commit (`main-merge-commit-only`, `gh pr merge <n> --merge`), which ALSO requires both `Review gate` and `CI gate` before merging and creating/pushing an annotated tag `vX.Y.Z` to trigger `release.yml` and `git-cliff` changelog/release publishing.
- Never push directly to `develop` or `main`. Merged PR head branches are automatically pruned on merge (`delete_branch_on_merge`); never manually delete unmerged or protected (`main`/`develop`) branches without confirmation.

### 3. Conventional Commits 1.0
- Required on all non-merge commits targeting `develop` and on all PR titles:
  `type(optional-scope): lowercase imperative summary`
- Allowed types: `feat`, `fix`, `docs`, `ci`, `chore`, `refactor`, `test`, `build`, `perf`, `style`.
- Header must be `<= 72` characters, start with a lowercase alphanumeric character after `: `, and have **no trailing period**.
- Explain *why* in the commit body when non-obvious. Use `!` or `BREAKING CHANGE:` only when an existing user's `.env` or data layout would break.

### 4. Parity Rule
Every change that adds, renames, or removes a service, environment variable, or port **must** update all four locations together:
1. `.env` — With a `# <service> Configuration` section comment.
2. `README.md` `.env` example block — Must mirror `.env` `KEY=VALUE` entries identically and in the exact same order.
3. `README.md` service section — Description, access URL, and any setup, upgrade, or migration notes.
4. `apps/home-server-dashboard/html/index.html` — Update the `services` array whenever the service has a web UI (or note non-HTTP ports).

### 5. Security & Reliability
- **Secrets:** Use `CHANGE_TO_<NAME>` placeholders in `.env` and `README.md`. Never commit real credentials, tokens, or personal IPs.
- **Image pinning:** Pin new services to explicit stable version tags (avoid `:latest`).
- **Least privilege:** Run containers as non-root (`PUID`/`PGID` or `user: "${DEFAULT_PUID}:${DEFAULT_GUID}"`) where supported. Never mount `/var/run/docker.sock`.
- **Experimental services:** Gate opt-in or experimental services behind `profiles: ["beta"]`.
- **Healthchecks:** Every service in `docker-compose.yml` must define a verified `healthcheck` (`test`, `interval`, `timeout`, `retries`), enforced automatically by `pytest`.
- **Formatting:** 2-space YAML indentation, no trailing whitespace, newline at EOF. Shell scripts must use `#!/usr/bin/env bash`, `set -euo pipefail`, be idempotent, and pass `shellcheck`.

### 6. Verification Before Opening a PR
Run the local quality suite and empirical container checks before pushing or opening a PR:
- `make check` (runs `lint`, `validate`, and `test`) — or the underlying tools directly (`yamllint`, `shellcheck`, `DEFAULT_STORAGE_MOUNT=/tmp/homelab-storage docker compose config --quiet`, `python3 -m pytest -q tests`).
- `make qa` / `make system-test` — runs `scripts/system-test.sh` + `scripts/qa-test.py` end-to-end (`make up-beta` with `tests/docker-compose.ci.yml` in an isolated temp workspace, verifies all 16 services reach `healthy`, checks dual-interface reachability, audits every live Dashboard card link, runs Hermes Web UI login + Compose DNS checks, runs per-service functional/security assertions, and tears down with `make down-beta`); also enforced in CI via the `System QA Test (make up-beta)` stage.
- Empirical Docker verification (building/pulling/starting modified services and verifying HTTP/health endpoints against temporary directories, then tearing down cleanly).

## Specialized Agents

| Agent | Tier | Access | Scope | Definition |
| :--- | :--- | :--- | :--- | :--- |
| **Developer** | `high` | `read-write` | Feature and bugfix implementation in isolated worktrees | [`.agents/agents/developer.md`](.agents/agents/developer.md) |
| **Reviewer** | `high` | `read-only` | PR code, infra, parity, and security review | [`.agents/agents/reviewer.md`](.agents/agents/reviewer.md) |
| **Maintainer** | `high` | `repo-maintainer` | Triage, merging, releases, dependency hygiene, branch protection | [`.agents/agents/maintainer.md`](.agents/agents/maintainer.md) |
| **Researcher** | `high` | `read-only-web` | Self-hosted service evaluation and grounded architecture specs | [`.agents/agents/researcher.md`](.agents/agents/researcher.md) |

See [`.agents/README.md`](.agents/README.md) for invocation patterns and model-tier guidance.
