# Contributing to HomeLab

Thanks for contributing! This document outlines the branching model, commit conventions, service/configuration rules, and local checks used in this repository.

## Branching & Release Model

- `main` holds production-ready releases.
- `develop` is the integration branch for all ongoing work.
- Create feature/fix/maintenance branches off `develop` using one of the following prefixes:
  - `feat/<kebab-case>` — new services or features
  - `fix/<kebab-case>` — bug fixes
  - `ci/<kebab-case>` — CI/CD and tooling changes
  - `docs/<kebab-case>` — documentation-only updates
  - `chore/<kebab-case>` — routine maintenance
- Open a pull request targeting `develop`.
- Before merging into `develop`, rebase your branch onto `origin/develop` so history stays clean and linear, then merge with a merge commit.
- **Releases**: Create a PR from `develop` → `main` and tag the release commit on `main` as `vX.Y.Z` (e.g., `v1.3.0`).

## Conventional Commits

PR titles and all non-merge commits targeting `develop` must follow [Conventional Commits 1.0](https://www.conventionalcommits.org/):

```text
<type>(<optional-scope>): <lowercase imperative summary>
```

Allowed types: `feat`, `fix`, `docs`, `ci`, `chore`, `refactor`, `test`, `build`, `perf`, `style`.

Rules:
- Keep the header at or under 72 characters.
- Start the summary with lowercase and omit a trailing period.
- Use the commit body to explain **why** the change was made when helpful.
- Use `!` or a `BREAKING CHANGE:` footer only if existing `.env` configurations will break.

Examples:
- `feat(radarr): pin image tag and add healthcheck`
- `fix(setup): safely load .env variables with special characters`
- `ci: add quality gate workflow and test suite`
- `docs(readme): document seerr port configuration`

> Note: Release PRs (`develop` → `main`) validate the PR title only, allowing older non-conventional history on `develop` to merge cleanly into `main`.

## Service & Environment Change Checklist

Every change that adds, renames, or removes an environment variable, service, or port **must** update all of the following in sync:

1. **`.env`** — add/update placeholders (`CHANGE_TO_<NAME>` for secrets; never commit real secrets) under a `# <service> Configuration` section comment.
2. **`README.md` `.env` example block** — the ` ```yml ` block in Installation must mirror `.env` key-for-key, value-for-value, and in the exact same order (enforced automatically by `pytest`).
3. **`README.md` service section** — document the service description, access URL/port, and any upgrade or migration notes.
4. **Dashboard (`apps/home-server-dashboard/html/index.html`)** — add or update the entry in the `services` array when the service provides a web UI.

## Local Checks

Install the test dependencies (once):

```bash
python3 -m pip install --user -r tests/requirements.txt
```

Run the full quality gate locally before pushing:

```bash
make check
```

Individual targets are also available:
- `make lint` — run `yamllint`, `shellcheck`, and `hadolint` (if installed)
- `make validate` — validate `docker-compose.yml` (using a temporary `DEFAULT_STORAGE_MOUNT` if `.env` still has the placeholder)
- `make test` — run the `pytest` validation suite (`tests/`)
