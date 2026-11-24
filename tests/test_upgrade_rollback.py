from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
MERGE_ENV_SCRIPT = SCRIPTS_DIR / "merge-env.py"
BACKUP_SCRIPT = SCRIPTS_DIR / "backup.sh"
ROLLBACK_SCRIPT = SCRIPTS_DIR / "rollback.sh"
UPGRADE_SCRIPT = SCRIPTS_DIR / "upgrade.sh"

LEGACY_COMMIT = "ec3a6250bfa6d6d1391e1a14100b3342ecef2cc5"


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        capture_output=True,
        text=True,
        check=check,
    )


def _has_commit(commit_sha: str) -> bool:
    res = _run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{commit_sha}^{{commit}}"],
        check=False,
    )
    return res.returncode == 0


def _seed_legacy_repo_clone(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Create an isolated clone checked out at LEGACY_COMMIT with realistic secrets & data.

    Returns (repo_dir, bootstrap_dir, storage_dir, backup_parent_dir).
    """
    if not _has_commit(LEGACY_COMMIT):
        pytest.skip(f"Historical commit {LEGACY_COMMIT} not present in shallow clone")

    repo_dir = tmp_path / "HomeLab"
    _run(["git", "clone", "--no-hardlinks", str(REPO_ROOT), str(repo_dir)])
    _run(["git", "-C", str(repo_dir), "config", "user.name", "Test User"])
    _run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"])

    # Create a local target branch containing the working tree state of REPO_ROOT
    # so bootstrap tests work even before commits are pushed.
    _run(["git", "-C", str(repo_dir), "checkout", "-B", "target-release", "HEAD"])
    for rel in ("scripts/merge-env.py", "scripts/backup.sh", "scripts/rollback.sh", "scripts/upgrade.sh"):
        dst = repo_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dst)
        dst.chmod(0o755)
    shutil.copy2(REPO_ROOT / ".env", repo_dir / ".env")
    _run(["git", "-C", str(repo_dir), "add", "scripts", ".env"])
    diff_check = _run(["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"], check=False)
    if diff_check.returncode != 0:
        _run(["git", "-C", str(repo_dir), "commit", "-m", "chore: stage target scripts"])

    # Check out historical commit ec3a6250 to simulate the live production server
    _run(["git", "-C", str(repo_dir), "checkout", "-B", "main", LEGACY_COMMIT])

    storage_dir = tmp_path / "storage"
    (storage_dir / "maintainerr" / "data").mkdir(parents=True, exist_ok=True)
    (storage_dir / "maintainerr" / "data" / "maintainerr.sqlite").write_text(
        "sqlite-maint-v1", encoding="utf-8"
    )

    # Populate realistic live .env with special-character secrets & custom/legacy values
    env_text = (repo_dir / ".env").read_text(encoding="utf-8")
    replacements = {
        "DEFAULT_STORAGE_MOUNT=CHANGE_TO_STORAGE_PATH": f"DEFAULT_STORAGE_MOUNT={storage_dir}",
        "DEFAULT_TIMEZONE=CHANGE_TO_YOUR_TIMEZONE": "DEFAULT_TIMEZONE=Asia/Jerusalem",
        "DEFAULT_LAN_IP=CHANGE_TO_YOUR_LAN_IP": "DEFAULT_LAN_IP=10.0.0.32",
        "SAMBA_USERNAME=CHANGE_TO_SAMBA_USERNAME": "SAMBA_USERNAME=live_smb_user",
        "SAMBA_PASSWORD=CHANGE_TO_SAMBA_PASSWORD": "SAMBA_PASSWORD=s3cr3t$pa&ss;with spaces!",
        "COUCHDB_USER=CHANGE_TO_COUCHDB_USER": "COUCHDB_USER=obsidian_user",
        "COUCHDB_PASSWORD=CHANGE_TO_COUCHDB_PASSWORD": "COUCHDB_PASSWORD='cdb$secret&1;2'",
        "PI_HOLE_WEB_PASSWORD=CHANGE_TO_PI_HOLE_WEB_PASSWORD": "PI_HOLE_WEB_PASSWORD=pi#hole$pass&word",
        "MATTERMOST_DB_USER=CHANGE_TO_MATTERMOST_DB_USER": "MATTERMOST_DB_USER=mm_legacy_user",
        "MATTERMOST_DB_PASSWORD=CHANGE_TO_MATTERMOST_DB_PASSWORD": "MATTERMOST_DB_PASSWORD=mm$legacy_pw",
    }
    for old, new in replacements.items():
        assert old in env_text, f"Expected line {old!r} in legacy .env"
        env_text = env_text.replace(old, new)
    env_text += "\nCUSTOM_OPERATOR_VAR=keep_me$safe\n"
    (repo_dir / ".env").write_text(env_text, encoding="utf-8")
    (repo_dir / ".env").chmod(0o600)

    # Seed realistic persistent service data in ./apps alongside git-tracked home-server-dashboard
    apps_dir = repo_dir / "apps"
    (apps_dir / "plex" / "config").mkdir(parents=True, exist_ok=True)
    (apps_dir / "plex" / "config" / "Preferences.xml").write_text("<Preferences token='abc$1'/>", encoding="utf-8")
    (apps_dir / "couch-db" / "couchdb-data").mkdir(parents=True, exist_ok=True)
    (apps_dir / "couch-db" / "couchdb-data" / "notes.couch").write_bytes(b"\x00\x01couchdata\xff")
    (apps_dir / "seerr" / "config").mkdir(parents=True, exist_ok=True)
    (apps_dir / "seerr" / "config" / "settings.json").write_text('{"initialized": true}\n', encoding="utf-8")

    # Populate separate /tmp-style bootstrap directory with the new scripts
    bootstrap_dir = tmp_path / "hl-up"
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    for script_name in ("merge-env.py", "backup.sh", "rollback.sh", "upgrade.sh"):
        dst = bootstrap_dir / script_name
        shutil.copy2(REPO_ROOT / "scripts" / script_name, dst)
        dst.chmod(0o755)

    backup_parent = tmp_path / "backups"
    return repo_dir, bootstrap_dir, storage_dir, backup_parent


def test_scripts_exist_and_are_executable() -> None:
    for script in (MERGE_ENV_SCRIPT, BACKUP_SCRIPT, ROLLBACK_SCRIPT, UPGRADE_SCRIPT):
        assert script.is_file(), f"Missing expected script: {script}"
        mode = stat.S_IMODE(script.stat().st_mode)
        assert mode & stat.S_IXUSR, f"Script is not executable (0755 expected): {script}"


def test_merge_env_preserves_special_chars_migrates_and_never_leaks_secrets(tmp_path: Path) -> None:
    template = tmp_path / "template.env"
    current = tmp_path / "current.env"
    output = tmp_path / "output.env"

    template.write_text(
        "\n".join(
            [
                "# General Configuration",
                "DEFAULT_STORAGE_MOUNT=CHANGE_TO_STORAGE_PATH",
                "DEFAULT_LAN_IP=CHANGE_TO_YOUR_LAN_IP",
                "",
                "# seerr Configuration",
                "SEERR_FOLDER=seerr",
                "SEERR_PORT=5055",
                "SEERR_LOG_LEVEL=info",
                "",
                "# samba Configuration",
                "SAMBA_PASSWORD=CHANGE_TO_SAMBA_PASSWORD",
                "",
                "# hermes-agent Configuration (beta profile)",
                "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=CHANGE_TO_HERMES_DASHBOARD_PASSWORD",
                "HERMES_OPENROUTER_API_KEY=CHANGE_TO_OPENROUTER_API_KEY",
                "",
            ]
        ),
        encoding="utf-8",
    )

    secret_pw = "P@ss$w0rd&with;spaces'and\"quotes!"
    current.write_text(
        "\n".join(
            [
                "DEFAULT_STORAGE_MOUNT=/mnt/media",
                "DEFAULT_LAN_IP=192.168.1.42",
                "JELLYSEERR_FOLDER=custom-jellyseerr",
                "JELLYSEERR_PORT=5059",
                f"SAMBA_PASSWORD={secret_pw}",
                "MATTERMOST_DB_PASSWORD=old_mm_secret",
                "USER_CUSTOM_KEY=custom$val;1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # Without --beta, HERMES_* placeholders must NOT fail --check-placeholders
    res = _run(
        [
            "python3",
            str(MERGE_ENV_SCRIPT),
            "--template",
            str(template),
            "--current",
            str(current),
            "--output",
            str(output),
            "--check-placeholders",
        ]
    )
    combined_logs = res.stdout + res.stderr
    assert secret_pw not in combined_logs
    assert "old_mm_secret" not in combined_logs
    assert "/mnt/media" not in combined_logs
    assert "JELLYSEERR_FOLDER->SEERR_FOLDER" in res.stdout
    assert "JELLYSEERR_PORT->SEERR_PORT" in res.stdout

    merged_lines = output.read_text(encoding="utf-8").splitlines()
    assert "DEFAULT_STORAGE_MOUNT=/mnt/media" in merged_lines
    assert "DEFAULT_LAN_IP=192.168.1.42" in merged_lines
    assert "SEERR_FOLDER=custom-jellyseerr" in merged_lines
    assert "SEERR_PORT=5059" in merged_lines
    assert "SEERR_LOG_LEVEL=info" in merged_lines
    assert f"SAMBA_PASSWORD={secret_pw}" in merged_lines
    assert "# Preserved legacy / custom variables" in merged_lines
    assert "MATTERMOST_DB_PASSWORD=old_mm_secret" in merged_lines
    assert "USER_CUSTOM_KEY=custom$val;1" in merged_lines
    # Migrated source keys should not be duplicated in the legacy footer
    assert not any(line.startswith("JELLYSEERR_FOLDER=") for line in merged_lines)
    assert not any(line.startswith("JELLYSEERR_PORT=") for line in merged_lines)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    # With --beta, unresolved HERMES_DASHBOARD_BASIC_AUTH_PASSWORD fails (while optional
    # HERMES_OPENROUTER_API_KEY placeholder is allowed) and never leaks secrets
    beta_fail = _run(
        [
            "python3",
            str(MERGE_ENV_SCRIPT),
            "--template",
            str(template),
            "--current",
            str(current),
            "--output",
            str(tmp_path / "beta_out.env"),
            "--check-placeholders",
            "--beta",
        ],
        check=False,
    )
    assert beta_fail.returncode != 0
    assert "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" in beta_fail.stderr
    assert "HERMES_OPENROUTER_API_KEY" not in beta_fail.stderr
    assert secret_pw not in (beta_fail.stdout + beta_fail.stderr)
    assert not (tmp_path / "beta_out.env").exists()


def test_merge_env_does_not_overwrite_existing_non_placeholder_seerr_keys(tmp_path: Path) -> None:
    template = tmp_path / "template.env"
    current = tmp_path / "current.env"
    output = tmp_path / "output.env"

    template.write_text("SEERR_FOLDER=seerr\nSEERR_PORT=5055\n", encoding="utf-8")
    current.write_text(
        "JELLYSEERR_FOLDER=old_js\nJELLYSEERR_PORT=5056\nSEERR_FOLDER=already_seerr\nSEERR_PORT=6055\n",
        encoding="utf-8",
    )

    _run(
        [
            "python3",
            str(MERGE_ENV_SCRIPT),
            "--template",
            str(template),
            "--current",
            str(current),
            "--output",
            str(output),
        ]
    )
    text = output.read_text(encoding="utf-8")
    assert "SEERR_FOLDER=already_seerr\n" in text
    assert "SEERR_PORT=6055\n" in text
    assert "JELLYSEERR_FOLDER=old_js\n" in text


def test_e2e_bootstrap_upgrade_and_manual_rollback_from_ec3a6250(tmp_path: Path) -> None:
    repo_dir, bootstrap_dir, storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)
    pre_upgrade_env = (repo_dir / ".env").read_bytes()
    pre_upgrade_prefs = (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").read_bytes()
    pre_upgrade_couch = (repo_dir / "apps" / "couch-db" / "couchdb-data" / "notes.couch").read_bytes()
    pre_upgrade_maint = (storage_dir / "maintainerr" / "data" / "maintainerr.sqlite").read_bytes()

    env_vars = {"HOMELAB_BACKUP_DIR": str(backup_parent)}

    # Dry-run first: must succeed and leave repo completely untouched
    dry = _run(
        [
            "bash",
            str(bootstrap_dir / "upgrade.sh"),
            "--repo-dir",
            str(repo_dir),
            "--target",
            "target-release",
            "--skip-docker",
            "--skip-fetch",
            "--dry-run",
        ],
        env=env_vars,
    )
    assert "[dry-run]" in dry.stdout
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip() == LEGACY_COMMIT
    assert (repo_dir / ".env").read_bytes() == pre_upgrade_env

    # Live bootstrap upgrade from ec3a6250 -> target-release
    up = _run(
        [
            "bash",
            str(bootstrap_dir / "upgrade.sh"),
            "--repo-dir",
            str(repo_dir),
            "--target",
            "target-release",
            "--skip-docker",
            "--skip-fetch",
        ],
        env=env_vars,
    )
    assert "s3cr3t$pa&ss;with spaces!" not in (up.stdout + up.stderr)
    assert "Upgrade complete!" in up.stdout

    target_sha = _run(["git", "-C", str(repo_dir), "rev-parse", "target-release"]).stdout.strip()
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip() == target_sha

    upgraded_env = (repo_dir / ".env").read_text(encoding="utf-8")
    assert "SAMBA_PASSWORD=s3cr3t$pa&ss;with spaces!" in upgraded_env
    assert "COUCHDB_PASSWORD='cdb$secret&1;2'" in upgraded_env
    assert "PI_HOLE_WEB_PASSWORD=pi#hole$pass&word" in upgraded_env
    assert "SEERR_LOG_LEVEL=info" in upgraded_env
    assert "OLLAMA_CONTEXT_LENGTH=65536" in upgraded_env
    assert "MATTERMOST_DB_PASSWORD=mm$legacy_pw" in upgraded_env
    assert "CUSTOM_OPERATOR_VAR=keep_me$safe" in upgraded_env
    assert stat.S_IMODE((repo_dir / ".env").stat().st_mode) == 0o600

    # Verify backup contents and SHA256SUMS
    latest_backup = (backup_parent / "latest").resolve()
    assert latest_backup.is_dir()
    _run(["sha256sum", "-c", "SHA256SUMS"], cwd=latest_backup)
    apps_listing = _run(["tar", "-tzf", str(latest_backup / "apps.tar.gz")]).stdout
    assert "home-server-dashboard" not in apps_listing
    assert "plex/config/Preferences.xml" in apps_listing
    assert (latest_backup / "maintainerr-data.tar.gz").is_file()

    # App data & git-tracked dashboard remain intact after upgrade
    assert (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").read_bytes() == pre_upgrade_prefs
    assert (repo_dir / "apps" / "couch-db" / "couchdb-data" / "notes.couch").read_bytes() == pre_upgrade_couch
    assert (repo_dir / "apps" / "home-server-dashboard" / "html" / "index.html").is_file()

    # Mutate .env, apps/, and maintainerr data post-upgrade, then roll back manually
    (repo_dir / ".env").write_text("CORRUPTED=1\n", encoding="utf-8")
    (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").write_text("CORRUPTED", encoding="utf-8")
    (repo_dir / "apps" / "new-post-upgrade-dir").mkdir()
    (storage_dir / "maintainerr" / "data" / "maintainerr.sqlite").write_text("CORRUPTED", encoding="utf-8")

    rb = _run(
        [
            "bash",
            str(bootstrap_dir / "rollback.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup",
            str(latest_backup),
            "--skip-docker",
            "--yes",
        ],
        env=env_vars,
    )
    assert "Rollback completed successfully" in rb.stdout
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip() == LEGACY_COMMIT
    assert (repo_dir / ".env").read_bytes() == pre_upgrade_env
    assert stat.S_IMODE((repo_dir / ".env").stat().st_mode) == 0o600
    assert (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").read_bytes() == pre_upgrade_prefs
    assert (repo_dir / "apps" / "couch-db" / "couchdb-data" / "notes.couch").read_bytes() == pre_upgrade_couch
    assert (storage_dir / "maintainerr" / "data" / "maintainerr.sqlite").read_bytes() == pre_upgrade_maint
    assert not (repo_dir / "apps" / "new-post-upgrade-dir").exists()
    assert (repo_dir / "apps" / "home-server-dashboard" / "html" / "index.html").is_file()


def test_automatic_rollback_on_upgrade_failure(tmp_path: Path) -> None:
    repo_dir, bootstrap_dir, storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)

    # Revert PI_HOLE_WEB_PASSWORD back to placeholder so merge-env --check-placeholders fails post-checkout
    env_text = (repo_dir / ".env").read_text(encoding="utf-8")
    env_text = env_text.replace(
        "PI_HOLE_WEB_PASSWORD=pi#hole$pass&word",
        "PI_HOLE_WEB_PASSWORD=CHANGE_TO_PI_HOLE_WEB_PASSWORD",
    )
    (repo_dir / ".env").write_text(env_text, encoding="utf-8")
    pre_upgrade_env = (repo_dir / ".env").read_bytes()
    pre_upgrade_prefs = (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").read_bytes()
    pre_upgrade_maint = (storage_dir / "maintainerr" / "data" / "maintainerr.sqlite").read_bytes()

    fail = _run(
        [
            "bash",
            str(bootstrap_dir / "upgrade.sh"),
            "--repo-dir",
            str(repo_dir),
            "--target",
            "target-release",
            "--skip-docker",
            "--skip-fetch",
        ],
        env={"HOMELAB_BACKUP_DIR": str(backup_parent)},
        check=False,
    )
    assert fail.returncode != 0
    assert "Initiating automatic rollback" in fail.stderr
    assert "Automatic rollback succeeded" in fail.stderr
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip() == LEGACY_COMMIT
    assert (repo_dir / ".env").read_bytes() == pre_upgrade_env
    assert (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").read_bytes() == pre_upgrade_prefs
    assert (storage_dir / "maintainerr" / "data" / "maintainerr.sqlite").read_bytes() == pre_upgrade_maint


def test_safety_guards_dirty_repo_backup_inside_repo_and_tampered_checksums(tmp_path: Path) -> None:
    repo_dir, bootstrap_dir, _storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)
    pre_env = (repo_dir / ".env").read_bytes()

    # 1. Refuse backup directory inside repository
    inside_backup = _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(repo_dir / "my-backup"),
            "--skip-docker",
        ],
        check=False,
    )
    assert inside_backup.returncode != 0
    assert "must NOT be inside repository" in inside_backup.stderr

    # 2. Refuse upgrade if any tracked file OTHER than .env is dirty
    readme = repo_dir / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n# dirty\n", encoding="utf-8")
    dirty_up = _run(
        [
            "bash",
            str(bootstrap_dir / "upgrade.sh"),
            "--repo-dir",
            str(repo_dir),
            "--target",
            "target-release",
            "--skip-docker",
            "--skip-fetch",
        ],
        env={"HOMELAB_BACKUP_DIR": str(backup_parent)},
        check=False,
    )
    assert dirty_up.returncode != 0
    assert "README.md" in dirty_up.stderr
    _run(["git", "-C", str(repo_dir), "checkout", "--", "README.md"])

    # 3. Create valid backup, then tamper with .env inside backup; rollback must abort cleanly
    good_backup_dir = backup_parent / "good-snap"
    _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(good_backup_dir),
            "--skip-docker",
        ]
    )

    # Upgrade cleanly first so repo state is clearly distinct
    _run(
        [
            "bash",
            str(bootstrap_dir / "upgrade.sh"),
            "--repo-dir",
            str(repo_dir),
            "--target",
            "target-release",
            "--skip-docker",
            "--skip-fetch",
        ],
        env={"HOMELAB_BACKUP_DIR": str(backup_parent)},
    )
    upgraded_sha = _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip()
    upgraded_env = (repo_dir / ".env").read_bytes()
    assert upgraded_env != pre_env

    # Tamper with backed-up .env
    (good_backup_dir / ".env").chmod(0o600)
    (good_backup_dir / ".env").write_text("TAMPERED=1\n", encoding="utf-8")

    tampered_rb = _run(
        [
            "bash",
            str(bootstrap_dir / "rollback.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup",
            str(good_backup_dir),
            "--skip-docker",
            "--yes",
        ],
        check=False,
    )
    assert tampered_rb.returncode != 0
    # Repo HEAD and .env must remain completely untouched after checksum failure
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip() == upgraded_sha
    assert (repo_dir / ".env").read_bytes() == upgraded_env
