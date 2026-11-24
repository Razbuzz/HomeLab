# Safe Backup, Upgrade, and Rollback Guide

HomeLab includes an automated, zero-data-loss workflow to back up configuration and persistent service data, merge `.env` templates non-destructively, validate Docker Compose and volume permissions, verify live container health, and automatically roll back on any failure.

> [!IMPORTANT]
> Because `.env` is tracked in git, running a bare `git pull` or `git checkout` on a configured server can conflict with or overwrite local secrets. Always use `make upgrade` (or the bootstrap procedure below for servers on pre-`v1.5.0` commits).

---

## Routine Operations (`v1.5.0+`)

### Create a standalone backup

```bash
make backup
# Optional custom destination:
make backup BACKUP=/mnt/backups/homelab-manual
```

### Upgrade to the latest release (or a specific ref)

```bash
# Preview target version, disk headroom, and .env merge without modifying anything:
bash scripts/upgrade.sh --dry-run

# Upgrade to the latest semver tag (v*.*.*):
make upgrade

# Upgrade to a specific tag or branch:
make upgrade TARGET=v1.5.0
make upgrade TARGET=main

# Upgrade including the optional Hermes Agent beta profile:
bash scripts/upgrade.sh --beta
```

### Roll back manually to a verified backup

```bash
# Roll back to the most recent backup (<backup_parent>/latest):
make rollback

# Roll back to a specific snapshot non-interactively:
bash scripts/rollback.sh --backup /mnt/homelab-backups/20261124T061500Z-ec3a6250 --yes
```

---

## Upgrading from `ec3a6250` or Any Pre-`v1.5.0` Commit

Older commits such as `ec3a6250bfa6d6d1391e1a14100b3342ecef2cc5` do not yet include `scripts/upgrade.sh`, `scripts/backup.sh`, `scripts/rollback.sh`, or `scripts/merge-env.py`. You can **bootstrap safely** by extracting the four upgrade scripts from the target tag into `/tmp/hl-up/` and running them against your live clone without touching git working tree state first:

1. **Fetch tags from GitHub** (read-only — does not modify working files or `.env`):

   ```bash
   cd /mnt/HomeLab   # or your actual HomeLab clone path
   git fetch origin --tags --prune
   ```

2. **Extract the upgrade scripts from the target release tag into `/tmp/hl-up`** (or set `TARGET_REF=v1.5.0` or `TARGET_REF=origin/main` explicitly):

   ```bash
   TARGET_REF="$(git tag --list 'v*.*.*' --sort=-v:refname | head -n 1)"
   mkdir -p /tmp/hl-up
   for f in backup.sh rollback.sh upgrade.sh merge-env.py; do
     git show "${TARGET_REF}:scripts/${f}" > "/tmp/hl-up/${f}"
   done
   chmod 0755 /tmp/hl-up/backup.sh /tmp/hl-up/rollback.sh /tmp/hl-up/upgrade.sh /tmp/hl-up/merge-env.py
   ```

3. **Run a non-destructive dry-run first** to verify disk space, inspect `.env` additions/migrations, and confirm zero unexpected dirty files:

   ```bash
   bash /tmp/hl-up/upgrade.sh --repo-dir /mnt/HomeLab --target "${TARGET_REF}" --dry-run
   ```

4. **Execute the live upgrade** (takes a crash-consistent backup outside `/mnt/HomeLab`, merges `.env`, repairs permissions, starts the stack, and verifies all healthchecks):

   ```bash
   bash /tmp/hl-up/upgrade.sh --repo-dir /mnt/HomeLab --target "${TARGET_REF}"
   ```

5. **Clean up `/tmp/hl-up` once satisfied** (future upgrades can simply run `make upgrade`):

   ```bash
   rm -rf /tmp/hl-up
   ```

> [!TIP]
> If the dry-run reports uncommitted changes in tracked files **other than `.env`**, review them with `git status` and `git diff` and commit/stash/revert them before running the live upgrade. Local edits in `.env` are expected and handled automatically.

---

## What Gets Backed Up vs. Excluded

Backups are stored by default outside the repository at `${HOMELAB_BACKUP_DIR:-$(dirname "$REPO_DIR")/homelab-backups}/<UTC_TIMESTAMP>-<SHORT_SHA>` with a `latest` symlink pointing to the newest snapshot (when using `--backup-dir`, `latest` is not modified outside the default backup root).

| Item | Included | Notes |
| :--- | :--- | :--- |
| `.env` | Yes (`0600`) | Copied verbatim and SHA-256 verified before & after copy. |
| `metadata.env` | Yes (`0600`) | Records `GIT_COMMIT`, `GIT_BRANCH`, `GIT_TAG`, mount paths, `HAD_BETA_RUNNING`, and `ENV_SHA256`. |
| `${DEFAULT_APPS_MOUNT}` | Yes (`apps.tar.gz`) | Archived with `tar --numeric-owner` so non-root UIDs/GIDs (CouchDB, Seerr, etc.) stay intact. |
| `apps/home-server-dashboard` | **Excluded** | Tracked in git and rebuilt from the checked-out commit on upgrade/rollback. |
| `${DEFAULT_STORAGE_MOUNT}/${MAINTAINERR_FOLDER}/data` | Yes (`maintainerr-data.tar.gz`) | Backed up automatically whenever `DEFAULT_STORAGE_MOUNT` is configured and the directory exists. |
| Large media libraries (`movies`, `tv`, `downloads`) | **Excluded** | Only application configs/databases in `apps` and Maintainerr data are archived; media files on `DEFAULT_STORAGE_MOUNT` are untouched. |
| `SHA256SUMS` | Yes (`0600`) | Verified via `sha256sum -c SHA256SUMS` at backup completion and before every rollback. |

Before stopping containers or writing files, `backup.sh` also verifies:
- `APPS_MOUNT` exists, and the backup directory is neither inside nor an ancestor of `REPO_DIR`, `APPS_MOUNT`, or `MAINTAINERR_DATA_DIR` (and any existing custom `--backup-dir` is empty).
- The destination filesystem has enough free space (`du -sk` of sources + **20% headroom**).
- Unless `--online` or `--skip-docker` is passed, containers are stopped gracefully (`docker compose --profile '*' down`) for crash-consistent SQLite/CouchDB backups and restarted afterwards (unless `--no-restart` on a successful backup; mid-backup failures always restart stopped containers).

---

## `.env` Merge Rules (`scripts/merge-env.py`)

When upgrading across releases, `upgrade.sh` merges your backed-up `.env` into the new release's `.env` template:

1. **Template structure preserved:** Comments, blank lines, and section ordering come from the new release template.
2. **Verbatim user values:** All matching keys retain your existing values character-for-character (preserving `$`, `&`, `;`, quotes, and spaces without shell evaluation). Secret values are **never** printed to stdout or logs.
3. **New keys adopted automatically:** Newly added configuration keys (for example `SEERR_LOG_LEVEL=info` or `OLLAMA_CONTEXT_LENGTH=65536`) receive the new template's default value.
4. **Safe key renames/migrations:**
   - `JELLYSEERR_FOLDER` → `SEERR_FOLDER`
   - `JELLYSEERR_PORT` → `SEERR_PORT`
   (Applied only when the old key has a real non-placeholder value and the target key is absent or still a `CHANGE_TO_*` placeholder.)
5. **Legacy & custom keys preserved:** Keys present in your old `.env` that were removed from the template (e.g. `MATTERMOST_*`, `THREADFIN_*`) or custom variables you added are appended under `# Preserved legacy / custom variables` so nothing is silently lost.
6. **Placeholder safety check:** Required keys still starting with `CHANGE_TO_` fail the upgrade before restarting containers (beta `HERMES_*` placeholders are ignored unless `--beta` is passed).

---

## Automatic and Manual Rollback Behavior

If any step inside `upgrade.sh` fails after the backup completes — including an unresolved `CHANGE_TO_*` placeholder, invalid Compose config, failing permission repair, container crash (`exited`/`dead`), `unhealthy` status, or healthcheck timeout — `upgrade.sh`:

1. Prints the last 60 lines of logs for any failing container.
2. Automatically invokes `scripts/rollback.sh --backup "${BACKUP_PATH}" --yes` (unless `--no-auto-rollback` was passed).
3. Verifies `SHA256SUMS` and tar archive readability before touching git or host directories.
4. Stops containers (`--remove-orphans`), checks out the exact pre-upgrade `GIT_COMMIT` / `GIT_BRANCH`, restores `.env` (`0600`) with SHA-256 verification, atomically swaps `${APPS_MOUNT}` (preserving `home-server-dashboard`) and Maintainerr data back from archives, and starts the restored stack **without `--pull always`** so your pre-upgrade container images are reused immediately.
