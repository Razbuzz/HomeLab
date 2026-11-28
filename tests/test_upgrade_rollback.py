from __future__ import annotations

import hashlib
import io
import os
import shutil
import signal
import stat
import subprocess
import tarfile
import time
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
                "# vllm Configuration (vllm profile)",
                "VLLM_MODEL=Qwen/Qwen3-8B-AWQ",
                "VLLM_API_KEY=CHANGE_TO_VLLM_API_KEY",
                "VLLM_HF_TOKEN=CHANGE_TO_HF_TOKEN",
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

    # Without --beta / --vllm, HERMES_* and VLLM_* placeholders must NOT fail --check-placeholders
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
    # HERMES_OPENROUTER_API_KEY and VLLM_* placeholders are allowed) and never leaks secrets
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
    assert "VLLM_API_KEY" not in beta_fail.stderr
    assert "VLLM_HF_TOKEN" not in beta_fail.stderr
    assert secret_pw not in (beta_fail.stdout + beta_fail.stderr)
    assert not (tmp_path / "beta_out.env").exists()

    # With --vllm, unresolved VLLM_API_KEY fails (while optional VLLM_HF_TOKEN is allowed)
    vllm_fail = _run(
        [
            "python3",
            str(MERGE_ENV_SCRIPT),
            "--template",
            str(template),
            "--current",
            str(current),
            "--output",
            str(tmp_path / "vllm_out.env"),
            "--check-placeholders",
            "--vllm",
        ],
        check=False,
    )
    assert vllm_fail.returncode != 0
    assert "VLLM_API_KEY" in vllm_fail.stderr
    assert "VLLM_HF_TOKEN" not in vllm_fail.stderr
    assert "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" not in vllm_fail.stderr
    assert secret_pw not in (vllm_fail.stdout + vllm_fail.stderr)
    assert not (tmp_path / "vllm_out.env").exists()


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
    meta = _parse_kv_file(latest_backup / "metadata.env")
    apps_archive_name = meta["APPS_ARCHIVE"]
    maint_archive_name = meta["MAINTAINERR_ARCHIVE"]
    assert (latest_backup / apps_archive_name).is_file()
    assert (latest_backup / maint_archive_name).is_file()
    apps_listing = _list_tar_entries(latest_backup / apps_archive_name)
    assert "home-server-dashboard" not in apps_listing
    assert "plex/config/Preferences.xml" in apps_listing

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


def test_merge_env_repeated_run_idempotency(tmp_path: Path) -> None:
    template = tmp_path / "template.env"
    initial_current = tmp_path / "initial.env"
    merged_once = tmp_path / "merged_once.env"
    merged_twice = tmp_path / "merged_twice.env"

    template.write_text(
        "\n".join(
            [
                "# General Configuration",
                "DEFAULT_STORAGE_MOUNT=CHANGE_TO_STORAGE_PATH",
                "",
                "# seerr Configuration",
                "SEERR_FOLDER=seerr",
                "SEERR_PORT=5055",
                "SEERR_LOG_LEVEL=info",
                "",
            ]
        ),
        encoding="utf-8",
    )
    initial_current.write_text(
        "\n".join(
            [
                "DEFAULT_STORAGE_MOUNT=/mnt/media",
                "JELLYSEERR_FOLDER=custom-seerr",
                "MATTERMOST_DB_PASSWORD=old$secret;1",
                "CUSTOM_VAR=keep_me",
                "",
            ]
        ),
        encoding="utf-8",
    )

    _run(
        [
            "python3",
            str(MERGE_ENV_SCRIPT),
            "--template",
            str(template),
            "--current",
            str(initial_current),
            "--output",
            str(merged_once),
            "--check-placeholders",
        ]
    )
    _run(
        [
            "python3",
            str(MERGE_ENV_SCRIPT),
            "--template",
            str(template),
            "--current",
            str(merged_once),
            "--output",
            str(merged_twice),
            "--check-placeholders",
        ]
    )

    once_bytes = merged_once.read_bytes()
    twice_bytes = merged_twice.read_bytes()
    assert once_bytes == twice_bytes
    once_text = once_bytes.decode("utf-8")
    assert once_text.count("# Preserved legacy / custom variables") == 1
    assert "MATTERMOST_DB_PASSWORD=old$secret;1\n" in once_text
    assert "CUSTOM_VAR=keep_me\n" in once_text


def test_rollback_detects_foreign_uids_and_recovers_staged_data_without_sudo(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("Foreign-UID non-root elevation guard only applies when running as non-root")

    repo_dir, bootstrap_dir, _storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)
    backup_dir = backup_parent / "foreign-uid-snap"

    _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(backup_dir),
            "--skip-docker",
        ]
    )

    meta = _parse_kv_file(backup_dir / "metadata.env")
    orig_apps_archive = meta["APPS_ARCHIVE"]
    orig_maint_archive = meta.get("MAINTAINERR_ARCHIVE", "")
    # Replace apps.tar.gz (explicitly testing .tar.gz foreign-UID detection as well)
    # and update metadata.env + SHA256SUMS accordingly.
    if orig_apps_archive != "apps.tar.gz":
        (backup_dir / orig_apps_archive).unlink()
        meta_text = (backup_dir / "metadata.env").read_text(encoding="utf-8")
        meta_text = meta_text.replace(f"APPS_ARCHIVE={orig_apps_archive}", "APPS_ARCHIVE=apps.tar.gz")
        meta_text = meta_text.replace("ARCHIVE_COMPRESSION=zstd", "ARCHIVE_COMPRESSION=gzip")
        (backup_dir / "metadata.env").write_text(meta_text, encoding="utf-8")

    synthetic_tar = backup_dir / "apps.tar.gz"
    with tarfile.open(synthetic_tar, "w:gz") as tf:
        ti = tarfile.TarInfo("./couch-db/couchdb-data/foreign.couch")
        payload = b"foreign-uid-payload"
        ti.size = len(payload)
        ti.uid = 5984
        ti.gid = 5984
        ti.mode = 0o644
        tf.addfile(ti, io.BytesIO(payload))
    synthetic_tar.chmod(0o600)

    checksum_lines: list[str] = []
    for fname in (".env", "metadata.env", "apps.tar.gz", orig_maint_archive):
        if not fname:
            continue
        fpath = backup_dir / fname
        if fpath.is_file():
            digest = hashlib.sha256(fpath.read_bytes()).hexdigest()
            checksum_lines.append(f"{digest}  {fname}")
    (backup_dir / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

    pre_rollback_prefs = (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").read_bytes()

    # Run rollback with a restricted PATH containing essential tools EXCEPT sudo, simulating
    # a non-root environment where sudo is unavailable.
    fake_bin = tmp_path / "no-sudo-bin"
    fake_bin.mkdir()
    for tool in ("bash", "git", "tar", "gzip", "zstd", "sha256sum", "awk", "realpath", "dirname", "basename", "mktemp", "mv", "rm", "mkdir", "chmod", "cp", "id", "cat", "grep", "tail", "cut", "tee", "python3", "find"):
        found = shutil.which(tool)
        if found:
            os.symlink(found, fake_bin / tool)

    rb = _run(
        [
            "bash",
            str(bootstrap_dir / "rollback.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup",
            str(backup_dir),
            "--skip-docker",
            "--yes",
        ],
        env={"PATH": str(fake_bin)},
        check=False,
    )
    assert rb.returncode != 0
    assert "root privileges or 'sudo' are required" in rb.stderr
    # Staged apps/ data must be restored back into apps/ rather than left stranded or overwritten
    assert (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").read_bytes() == pre_rollback_prefs
    assert not list((repo_dir).glob(".homelab-restore-stage.*"))
    assert not (repo_dir / "apps" / "couch-db" / "couchdb-data" / "foreign.couch").exists()

    # Now add a shim sudo that records the exact command invoked for foreign-UID extraction
    # (stripping --same-owner only after logging so unprivileged pytest can complete extraction)
    sudo_log = tmp_path / "sudo-calls.log"
    shim_sudo = fake_bin / "sudo"
    shim_sudo.write_text(
        f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{sudo_log}"
if [[ "$1" == "tar" ]]; then
  args=()
  for a in "${{@:2}}"; do
    [[ "$a" == "--same-owner" ]] && continue
    args+=("$a")
  done
  exec "{shutil.which('tar')}" "${{args[@]}}"
fi
exec "$@"
""",
        encoding="utf-8",
    )
    shim_sudo.chmod(0o755)
    rb_sudo = _run(
        [
            "bash",
            str(bootstrap_dir / "rollback.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup",
            str(backup_dir),
            "--skip-docker",
            "--yes",
        ],
        env={"PATH": str(fake_bin)},
    )
    assert "Rollback completed successfully" in rb_sudo.stdout
    sudo_calls = sudo_log.read_text(encoding="utf-8").splitlines()
    assert any(
        "tar -z --same-owner --numeric-owner -xpf" in line and "apps.tar.gz" in line
        for line in sudo_calls
    )
    assert (repo_dir / "apps" / "couch-db" / "couchdb-data" / "foreign.couch").read_bytes() == b"foreign-uid-payload"


def test_upgrade_target_branch_resolves_origin_over_stale_local_branch(tmp_path: Path) -> None:
    repo_dir, bootstrap_dir, _storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)

    # Create a bare "origin" remote where origin/main is ahead of local main (LEGACY_COMMIT)
    origin_dir = tmp_path / "origin.git"
    _run(["git", "init", "--bare", str(origin_dir)])
    _run(["git", "-C", str(repo_dir), "remote", "set-url", "origin", str(origin_dir)])
    _run(["git", "-C", str(repo_dir), "push", "origin", "target-release:refs/heads/main"])
    target_sha = _run(["git", "-C", str(repo_dir), "rev-parse", "target-release"]).stdout.strip()

    # Local 'main' is still at LEGACY_COMMIT and checked out
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip() == LEGACY_COMMIT
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "main"]).stdout.strip() == LEGACY_COMMIT

    env_vars = {"HOMELAB_BACKUP_DIR": str(backup_parent)}

    dry = _run(
        [
            "bash",
            str(bootstrap_dir / "upgrade.sh"),
            "--repo-dir",
            str(repo_dir),
            "--target",
            "main",
            "--skip-docker",
            "--dry-run",
        ],
        env=env_vars,
    )
    assert f"Target ref:     main ({target_sha[:8]})" in dry.stdout
    assert "Nothing to do" not in dry.stdout
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip() == LEGACY_COMMIT

    up = _run(
        [
            "bash",
            str(bootstrap_dir / "upgrade.sh"),
            "--repo-dir",
            str(repo_dir),
            "--target",
            "main",
            "--skip-docker",
        ],
        env=env_vars,
    )
    assert "Upgrade complete!" in up.stdout
    assert f"To:     {target_sha[:8]} (main)" in up.stdout
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip() == target_sha
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "main"]).stdout.strip() == target_sha


def test_backup_refuses_ancestor_nonempty_missing_apps_and_recovers_stack_on_failure(tmp_path: Path) -> None:
    repo_dir, bootstrap_dir, _storage_dir, _backup_parent = _seed_legacy_repo_clone(tmp_path)

    # 1. Refuse ancestor directory of repo / apps
    ancestor_res = _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(tmp_path),
            "--skip-docker",
        ],
        check=False,
    )
    assert ancestor_res.returncode != 0
    assert "must NOT be an ancestor" in ancestor_res.stderr

    # 2. Refuse existing non-empty custom backup directory (and do not touch its contents/permissions)
    nonempty_dir = tmp_path / "existing-backup"
    nonempty_dir.mkdir()
    (nonempty_dir / "keep.txt").write_text("do-not-touch", encoding="utf-8")
    nonempty_res = _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(nonempty_dir),
            "--skip-docker",
        ],
        check=False,
    )
    assert nonempty_res.returncode != 0
    assert "already exists and is not empty" in nonempty_res.stderr
    assert (nonempty_dir / "keep.txt").read_text(encoding="utf-8") == "do-not-touch"

    # 3. Validate missing APPS_MOUNT pre-flight (both dry-run and live) before touching Docker
    shutil.rmtree(repo_dir / "apps")
    missing_apps_res = _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(tmp_path / "unused-snap"),
            "--dry-run",
        ],
        check=False,
    )
    assert missing_apps_res.returncode != 0
    assert "APPS_MOUNT directory not found" in missing_apps_res.stderr

    # 4. Mid-backup failure with --no-restart must still restart the stopped stack during cleanup
    (repo_dir / "apps" / "plex").mkdir(parents=True, exist_ok=True)
    (repo_dir / "apps" / "plex" / "cfg.txt").write_text("ok", encoding="utf-8")

    docker_log = tmp_path / "docker-calls.log"
    fake_bin = tmp_path / "fake-docker-bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{docker_log}"
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    # Make tar fail mid-backup when invoked to create apps archive (while allowing other system tools)
    real_tar = shutil.which("tar")
    assert real_tar is not None
    fake_tar = fake_bin / "tar"
    fake_tar.write_text(
        f"""#!/usr/bin/env bash
for arg in "$@"; do
  if [[ "$arg" == *apps.tar* ]]; then
    exit 2
  fi
done
exec "{real_tar}" "$@"
""",
        encoding="utf-8",
    )
    fake_tar.chmod(0o755)

    custom_snap = tmp_path / "custom-snap"
    fail_backup = _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(custom_snap),
            "--no-restart",
        ],
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        check=False,
    )
    assert fail_backup.returncode != 0
    assert not custom_snap.exists()
    assert not (tmp_path / "latest").exists()
    logged_calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert any("compose --profile * down" in line for line in logged_calls)
    assert any("compose up -d --remove-orphans" in line for line in logged_calls)

    # Also verify custom --backup-dir does NOT create a 'latest' symlink in its parent on success
    fake_tar.unlink()
    ok_custom = _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(custom_snap),
            "--skip-docker",
        ]
    )
    assert "Backup verified successfully." in ok_custom.stdout
    assert not (tmp_path / "latest").exists()


def test_upgrade_sighup_and_double_sigterm_trigger_uninterrupted_rollback(tmp_path: Path) -> None:
    repo_dir, bootstrap_dir, storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)
    pre_env = (repo_dir / ".env").read_bytes()
    pre_prefs = (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").read_bytes()
    pre_maint = (storage_dir / "maintainerr" / "data" / "maintainerr.sqlite").read_bytes()

    # Fake docker binary that signals upgrade.sh with SIGHUP during `docker compose up`
    # (after backup succeeded and git + .env have already been mutated to target-release).
    fake_bin = tmp_path / "sighup-docker-bin"
    fake_bin.mkdir()
    marker = tmp_path / "up-invoked"
    pid_file = tmp_path / "upgrade.pid"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
if [[ "$*" == *"compose up"* ]]; then
  touch "{marker}"
  if [[ -f "{pid_file}" ]]; then
    kill -HUP "$(cat "{pid_file}")"
    sleep 5
  fi
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    # Launch upgrade.sh via bash wrapper that records its own PID before exec'ing upgrade.sh
    proc = subprocess.Popen(
        [
            "bash",
            "-c",
            f'echo $$ > "{pid_file}"; exec bash "{bootstrap_dir / "upgrade.sh"}" '
            f'--repo-dir "{repo_dir}" --target target-release --skip-fetch --no-pull',
        ],
        env={
            **os.environ,
            "HOMELAB_BACKUP_DIR": str(backup_parent),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _stdout, stderr = proc.communicate(timeout=30)
    assert marker.exists()
    assert proc.returncode != 0
    assert "Initiating automatic rollback" in stderr
    assert "Automatic rollback succeeded" in stderr
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip() == LEGACY_COMMIT
    assert (repo_dir / ".env").read_bytes() == pre_env
    assert (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").read_bytes() == pre_prefs
    assert (storage_dir / "maintainerr" / "data" / "maintainerr.sqlite").read_bytes() == pre_maint
    assert signal.SIGHUP.value == 1

    # Verify a second SIGTERM arriving while rollback.sh is already running does NOT abort rollback.
    marker2 = tmp_path / "up-invoked-2"
    pid_file2 = tmp_path / "upgrade2.pid"
    rb_started = tmp_path / "rb-started"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
if [[ "$*" == *"compose up"* ]]; then
  touch "{marker2}"
  if [[ -f "{pid_file2}" ]]; then
    kill -TERM "$(cat "{pid_file2}")"
    sleep 5
  fi
elif [[ "$*" == *"compose --profile * down --remove-orphans"* ]]; then
  if [[ -f "{pid_file2}" && ! -f "{rb_started}" ]]; then
    touch "{rb_started}"
    kill -TERM "$(cat "{pid_file2}")"
    sleep 0.2
  fi
fi
exit 0
""",
        encoding="utf-8",
    )
    proc2 = subprocess.Popen(
        [
            "bash",
            "-c",
            f'echo $$ > "{pid_file2}"; exec bash "{bootstrap_dir / "upgrade.sh"}" '
            f'--repo-dir "{repo_dir}" --target target-release --skip-fetch --no-pull',
        ],
        env={
            **os.environ,
            "HOMELAB_BACKUP_DIR": str(backup_parent),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _stdout2, stderr2 = proc2.communicate(timeout=30)
    assert marker2.exists()
    assert rb_started.exists()
    assert proc2.returncode != 0
    assert "Automatic rollback succeeded" in stderr2
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip() == LEGACY_COMMIT
    assert (repo_dir / ".env").read_bytes() == pre_env
    assert (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").read_bytes() == pre_prefs
    assert (storage_dir / "maintainerr" / "data" / "maintainerr.sqlite").read_bytes() == pre_maint
    assert time.monotonic() > 0


def _parse_kv_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        result[k.strip()] = v.strip()
    return result


def _list_tar_entries(archive_path: Path) -> str:
    name = archive_path.name
    if name.endswith(".tar.zst") or name.endswith(".tzst"):
        return _run(["tar", "-I", "zstd -dc", "-tf", str(archive_path)]).stdout
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return _run(["tar", "-tzf", str(archive_path)]).stdout
    return _run(["tar", "-tf", str(archive_path)]).stdout


def test_backup_excludes_heavy_paths_and_rollback_preserves_them_with_spaces(tmp_path: Path) -> None:
    repo_dir, bootstrap_dir, _storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)
    apps_dir = repo_dir / "apps"

    # Seed heavy regenerable directories + essential DBs/keys (including paths with spaces)
    ollama_model = apps_dir / "ollama" / "models" / "blobs" / "sha256-fake-model"
    ollama_model.parent.mkdir(parents=True, exist_ok=True)
    ollama_model.write_bytes(b"fake-gguf-weights-payload")

    ollama_key = apps_dir / "ollama" / "id_ed25519"
    ollama_key.write_text("PRIVATE_KEY_DATA", encoding="utf-8")

    vllm_hf_weight = apps_dir / "vllm" / "huggingface" / "hub" / "models--Qwen--Qwen3-8B-AWQ" / "model.safetensors"
    vllm_hf_weight.parent.mkdir(parents=True, exist_ok=True)
    vllm_hf_weight.write_bytes(b"fake-hf-safetensors-weights")

    vllm_jit_cache = apps_dir / "vllm" / "vllm" / "torch_compile_cache" / "artifact.bin"
    vllm_jit_cache.parent.mkdir(parents=True, exist_ok=True)
    vllm_jit_cache.write_bytes(b"fake-vllm-jit-cache")

    vllm_meta = apps_dir / "vllm" / "custom-note.txt"
    vllm_meta.write_text("KEEP_VLLM_SMALL_FILE", encoding="utf-8")

    plex_root = apps_dir / "plex" / "config" / "Library" / "Application Support" / "Plex Media Server"
    plex_thumb = plex_root / "Media" / "thumb.bif"
    plex_thumb.parent.mkdir(parents=True, exist_ok=True)
    plex_thumb.write_bytes(b"regenerable-thumbnail-bif")

    plex_cache = plex_root / "Cache" / "photo.jpg"
    plex_cache.parent.mkdir(parents=True, exist_ok=True)
    plex_cache.write_bytes(b"regenerable-cache")

    plex_logs = plex_root / "Logs" / "Plex Media Server.log"
    plex_logs.parent.mkdir(parents=True, exist_ok=True)
    plex_logs.write_text("plex log line", encoding="utf-8")

    plex_db = plex_root / "Plug-in Support" / "Databases" / "com.plexapp.plugins.library.db"
    plex_db.parent.mkdir(parents=True, exist_ok=True)
    plex_db.write_bytes(b"SQLITE_LIBRARY_DB_V1")

    snap_dir = backup_parent / "fast-snap"
    res = _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(snap_dir),
            "--skip-docker",
        ]
    )
    assert "Backup verified successfully." in res.stdout

    meta = _parse_kv_file(snap_dir / "metadata.env")
    archive_path = snap_dir / meta["APPS_ARCHIVE"]
    assert archive_path.is_file()
    assert "./ollama/models" in meta["APPS_EXCLUDED_PATHS"]
    assert "./vllm/huggingface" in meta["APPS_EXCLUDED_PATHS"]
    assert "./vllm/vllm" in meta["APPS_EXCLUDED_PATHS"]
    assert "./plex/config/Library/Application Support/Plex Media Server/Media" in meta["APPS_EXCLUDED_PATHS"]

    listing = _list_tar_entries(archive_path)
    assert "ollama/models" not in listing
    assert "vllm/huggingface" not in listing
    assert "vllm/vllm" not in listing
    assert "Plex Media Server/Media" not in listing
    assert "Plex Media Server/Cache" not in listing
    assert "Plex Media Server/Logs" not in listing
    assert "ollama/id_ed25519" in listing
    assert "vllm/custom-note.txt" in listing
    assert "Plug-in Support/Databases/com.plexapp.plugins.library.db" in listing

    # Mutate backed-up config/DB & delete key while keeping heavy excluded directories intact
    plex_db.write_bytes(b"CORRUPTED_DB")
    ollama_key.unlink()
    vllm_meta.unlink()
    (apps_dir / "post-upgrade-junk").mkdir()

    rb = _run(
        [
            "bash",
            str(bootstrap_dir / "rollback.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup",
            str(snap_dir),
            "--skip-docker",
            "--yes",
        ]
    )
    assert "Rollback completed successfully" in rb.stdout

    # Essential configs/DBs are restored and post-upgrade junk is gone
    assert plex_db.read_bytes() == b"SQLITE_LIBRARY_DB_V1"
    assert ollama_key.read_text(encoding="utf-8") == "PRIVATE_KEY_DATA"
    assert vllm_meta.read_text(encoding="utf-8") == "KEEP_VLLM_SMALL_FILE"
    assert not (apps_dir / "post-upgrade-junk").exists()

    # Excluded heavy directories (including paths with spaces) are preserved untouched in place
    assert ollama_model.read_bytes() == b"fake-gguf-weights-payload"
    assert vllm_hf_weight.read_bytes() == b"fake-hf-safetensors-weights"
    assert vllm_jit_cache.read_bytes() == b"fake-vllm-jit-cache"
    assert plex_thumb.read_bytes() == b"regenerable-thumbnail-bif"
    assert plex_cache.read_bytes() == b"regenerable-cache"
    assert plex_logs.read_text(encoding="utf-8") == "plex log line"


def test_backup_include_heavy_archives_models_and_media(tmp_path: Path) -> None:
    repo_dir, bootstrap_dir, _storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)
    apps_dir = repo_dir / "apps"

    ollama_model = apps_dir / "ollama" / "models" / "blobs" / "sha256-fake-model"
    ollama_model.parent.mkdir(parents=True, exist_ok=True)
    ollama_model.write_bytes(b"fake-gguf-weights-payload")

    plex_thumb = (
        apps_dir
        / "plex"
        / "config"
        / "Library"
        / "Application Support"
        / "Plex Media Server"
        / "Media"
        / "thumb.bif"
    )
    plex_thumb.parent.mkdir(parents=True, exist_ok=True)
    plex_thumb.write_bytes(b"regenerable-thumbnail-bif")

    snap_dir = backup_parent / "heavy-snap"
    res = _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(snap_dir),
            "--include-heavy",
            "--skip-docker",
        ]
    )
    assert "Backup verified successfully." in res.stdout

    meta = _parse_kv_file(snap_dir / "metadata.env")
    assert meta["APPS_EXCLUDED_PATHS"] == "./home-server-dashboard"
    listing = _list_tar_entries(snap_dir / meta["APPS_ARCHIVE"])
    assert "home-server-dashboard" not in listing
    assert "ollama/models/blobs/sha256-fake-model" in listing
    assert "Plex Media Server/Media/thumb.bif" in listing


def test_backup_and_rollback_no_compress_plain_tar(tmp_path: Path) -> None:
    repo_dir, bootstrap_dir, storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)
    pre_prefs = (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").read_bytes()
    pre_maint = (storage_dir / "maintainerr" / "data" / "maintainerr.sqlite").read_bytes()

    snap_dir = backup_parent / "plain-tar-snap"
    res = _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(snap_dir),
            "--no-compress",
            "--skip-docker",
        ]
    )
    assert "Backup verified successfully." in res.stdout

    meta = _parse_kv_file(snap_dir / "metadata.env")
    assert meta["ARCHIVE_COMPRESSION"] == "none"
    assert meta["APPS_ARCHIVE"] == "apps.tar"
    assert meta["MAINTAINERR_ARCHIVE"] == "maintainerr-data.tar"
    assert (snap_dir / "apps.tar").is_file()
    assert (snap_dir / "maintainerr-data.tar").is_file()
    _run(["sha256sum", "-c", "SHA256SUMS"], cwd=snap_dir)

    (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").write_text("CORRUPTED", encoding="utf-8")
    (storage_dir / "maintainerr" / "data" / "maintainerr.sqlite").write_text("CORRUPTED", encoding="utf-8")

    rb = _run(
        [
            "bash",
            str(bootstrap_dir / "rollback.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup",
            str(snap_dir),
            "--skip-docker",
            "--yes",
        ]
    )
    assert "Rollback completed successfully" in rb.stdout
    assert (repo_dir / "apps" / "plex" / "config" / "Preferences.xml").read_bytes() == pre_prefs
    assert (storage_dir / "maintainerr" / "data" / "maintainerr.sqlite").read_bytes() == pre_maint


def test_rollback_backwards_compatible_with_v1_5_0_metadata_without_excluded_paths(tmp_path: Path) -> None:
    repo_dir, bootstrap_dir, _storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)
    apps_dir = repo_dir / "apps"
    pre_prefs = (apps_dir / "plex" / "config" / "Preferences.xml").read_bytes()

    # Craft a v1.5.0-style backup directory without APPS_EXCLUDED_PATHS or ARCHIVE_COMPRESSION
    legacy_snap = backup_parent / "v150-snap"
    legacy_snap.mkdir(parents=True)
    shutil.copy2(repo_dir / ".env", legacy_snap / ".env")
    (legacy_snap / ".env").chmod(0o600)
    env_sha = hashlib.sha256((legacy_snap / ".env").read_bytes()).hexdigest()

    _run(
        [
            "tar",
            "--numeric-owner",
            "--anchored",
            "--exclude=./home-server-dashboard",
            "-C",
            str(apps_dir),
            "-czpf",
            str(legacy_snap / "apps.tar.gz"),
            ".",
        ]
    )
    (legacy_snap / "metadata.env").write_text(
        "\n".join(
            [
                "BACKUP_VERSION=1",
                "CREATED_AT_UTC=20261124T000000Z",
                f"REPO_DIR={repo_dir}",
                f"GIT_COMMIT={LEGACY_COMMIT}",
                "GIT_BRANCH=main",
                "GIT_TAG=",
                f"APPS_MOUNT={apps_dir}",
                "STORAGE_MOUNT=",
                "MAINTAINERR_FOLDER=maintainerr",
                "HAD_BETA_RUNNING=false",
                f"ENV_SHA256={env_sha}",
                "APPS_ARCHIVE=apps.tar.gz",
                "MAINTAINERR_ARCHIVE=",
                "",
            ]
        ),
        encoding="utf-8",
    )
    checksum_lines = []
    for fname in (".env", "metadata.env", "apps.tar.gz"):
        digest = hashlib.sha256((legacy_snap / fname).read_bytes()).hexdigest()
        checksum_lines.append(f"{digest}  {fname}")
    (legacy_snap / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

    # Mutate Preferences.xml while ensuring git-tracked home-server-dashboard stays intact
    (apps_dir / "plex" / "config" / "Preferences.xml").write_text("CORRUPTED", encoding="utf-8")
    dashboard_index = apps_dir / "home-server-dashboard" / "html" / "index.html"
    assert dashboard_index.is_file()

    rb = _run(
        [
            "bash",
            str(bootstrap_dir / "rollback.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup",
            str(legacy_snap),
            "--skip-docker",
            "--yes",
        ]
    )
    assert "Rollback completed successfully" in rb.stdout
    assert (apps_dir / "plex" / "config" / "Preferences.xml").read_bytes() == pre_prefs
    assert dashboard_index.is_file()


def test_upgrade_tolerates_transient_unhealthy_and_fails_fast_on_crashed_container(tmp_path: Path) -> None:
    repo_dir, bootstrap_dir, _storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)
    fake_bin = tmp_path / "mock-docker-bin"
    fake_bin.mkdir()

    # 1. Transient unhealthy on first inspect poll, then healthy on subsequent polls -> upgrade succeeds
    inspect_count_file = tmp_path / "inspect-count.txt"
    inspect_count_file.write_text("0", encoding="utf-8")
    docker_log = tmp_path / "docker-calls.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{docker_log}"
if [[ "$1" == "compose" ]]; then
  shift
  case "$1" in
    config)
      if [[ "$*" == *"--services"* ]]; then
        printf 'plex\\n'
      fi
      exit 0
      ;;
    ps)
      if [[ "$*" == *"-q"* ]]; then
        printf 'deadbeef1234567890\\n'
      fi
      exit 0
      ;;
    pull|up|down)
      exit 0
      ;;
  esac
elif [[ "$1" == "inspect" ]]; then
  fmt="$3"
  if [[ "$fmt" == *"State.Status"* ]]; then
    printf 'running\\n'
    exit 0
  elif [[ "$fmt" == *"State.Health"* ]]; then
    count="$(cat "{inspect_count_file}")"
    count=$((count + 1))
    printf '%s' "$count" > "{inspect_count_file}"
    if [[ "$count" -le 1 ]]; then
      printf 'unhealthy\\n'
    else
      printf 'healthy\\n'
    fi
    exit 0
  elif [[ "$fmt" == *"State.Running"* ]]; then
    printf 'false\\n'
    exit 0
  fi
elif [[ "$1" == "logs" ]]; then
  printf 'mock container log line\\n'
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    # Also provide a no-op sudo shim in PATH in case fix-permissions needs it
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_sudo.chmod(0o755)

    res_recover = _run(
        [
            "bash",
            str(bootstrap_dir / "upgrade.sh"),
            "--repo-dir",
            str(repo_dir),
            "--target",
            "target-release",
            "--skip-fetch",
            "--timeout",
            "20",
        ],
        env={
            "HOMELAB_BACKUP_DIR": str(backup_parent),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )
    assert "Upgrade complete!" in res_recover.stdout
    assert "plex(running/unhealthy)" in res_recover.stdout
    assert "All 1 service(s) are running and healthy." in res_recover.stdout
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert any(line == "compose pull" for line in calls)
    assert any(line == "compose up -d --build --remove-orphans" for line in calls)

    # 2. Reset repo to LEGACY_COMMIT and verify exited container still fails fast + triggers rollback
    pre_env_bytes = (repo_dir / ".env").read_bytes()
    _run(["git", "-C", str(repo_dir), "checkout", "HEAD", "--", ".env"])
    _run(["git", "-C", str(repo_dir), "checkout", "-B", "main", LEGACY_COMMIT])
    (repo_dir / ".env").write_bytes(pre_env_bytes)
    (repo_dir / ".env").chmod(0o600)
    fake_docker.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "compose" ]]; then
  shift
  case "$1" in
    config)
      if [[ "$*" == *"--services"* ]]; then
        printf 'plex\\n'
      fi
      exit 0
      ;;
    ps)
      if [[ "$*" == *"-q"* ]]; then
        printf 'deadbeef1234567890\\n'
      fi
      exit 0
      ;;
    pull|up|down)
      exit 0
      ;;
  esac
elif [[ "$1" == "inspect" ]]; then
  fmt="$3"
  if [[ "$fmt" == *"State.Status"* ]]; then
    printf 'exited\\n'
    exit 0
  elif [[ "$fmt" == *"State.Health"* ]]; then
    printf 'unhealthy\\n'
    exit 0
  fi
elif [[ "$1" == "logs" ]]; then
  printf 'fatal crash in plex\\n'
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    t0 = time.monotonic()
    res_crash = _run(
        [
            "bash",
            str(bootstrap_dir / "upgrade.sh"),
            "--repo-dir",
            str(repo_dir),
            "--target",
            "target-release",
            "--skip-fetch",
            "--timeout",
            "30",
        ],
        env={
            "HOMELAB_BACKUP_DIR": str(backup_parent),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        check=False,
    )
    elapsed = time.monotonic() - t0
    assert res_crash.returncode != 0
    assert elapsed < 15
    assert "entered failure state (status=exited" in res_crash.stderr
    assert "fatal crash in plex" in res_crash.stderr
    assert "Automatic rollback succeeded" in res_crash.stderr
    assert _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip() == LEGACY_COMMIT


def test_backup_sigterm_terminates_active_archive_command_cleanly(tmp_path: Path) -> None:
    repo_dir, bootstrap_dir, _storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)

    fake_bin = tmp_path / "sigterm-bin"
    fake_bin.mkdir()
    real_tar = shutil.which("tar")
    assert real_tar is not None

    tar_started = tmp_path / "tar-started"
    tar_pid_file = tmp_path / "tar-child.pid"
    fake_tar = fake_bin / "tar"
    fake_tar.write_text(
        f"""#!/usr/bin/env bash
for arg in "$@"; do
  if [[ "$arg" == *apps.tar* ]]; then
    echo $$ > "{tar_pid_file}"
    touch "{tar_started}"
    while true; do
      sleep 0.2
    done
  fi
done
exec "{real_tar}" "$@"
""",
        encoding="utf-8",
    )
    fake_tar.chmod(0o755)

    snap_dir = backup_parent / "sigterm-snap"
    proc = subprocess.Popen(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(snap_dir),
            "--skip-docker",
        ],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    deadline = time.monotonic() + 10.0
    while not tar_started.exists() and time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.05)

    assert tar_started.exists(), "fake tar did not start in time"
    tar_pid = int(tar_pid_file.read_text(encoding="utf-8").strip())

    proc.send_signal(signal.SIGTERM)
    _stdout, _stderr = proc.communicate(timeout=15)
    assert proc.returncode != 0
    assert not snap_dir.exists()

    # Verify the active child tar command was terminated and not left orphaned
    gone = False
    reap_deadline = time.monotonic() + 3.0
    while time.monotonic() < reap_deadline:
        try:
            os.kill(tar_pid, 0)
        except ProcessLookupError:
            gone = True
            break
        time.sleep(0.05)
    assert gone, f"tar child PID {tar_pid} survived SIGTERM cleanup"


def test_backup_broken_symlink_does_not_trigger_false_root_requirement(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("Non-root false-positive symlink test only applies when running as non-root")

    repo_dir, bootstrap_dir, _storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)
    broken_link = repo_dir / "apps" / "plex" / "config" / "broken-container-link"
    broken_link.symlink_to("/nonexistent/container/internal/target")

    # Provide a PATH without sudo: if path_needs_root false-positives on the broken symlink,
    # backup.sh would fail trying to find/use sudo.
    no_sudo_bin = tmp_path / "no-sudo-bin"
    no_sudo_bin.mkdir()
    for tool in (
        "bash",
        "git",
        "tar",
        "gzip",
        "pigz",
        "zstd",
        "sha256sum",
        "awk",
        "realpath",
        "dirname",
        "basename",
        "mktemp",
        "mkdir",
        "chmod",
        "cp",
        "rm",
        "mv",
        "ln",
        "id",
        "cat",
        "grep",
        "du",
        "df",
        "stat",
        "find",
        "date",
    ):
        found = shutil.which(tool)
        if found:
            os.symlink(found, no_sudo_bin / tool)

    snap_dir = backup_parent / "broken-symlink-snap"
    res = _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(snap_dir),
            "--skip-docker",
        ],
        env={"PATH": str(no_sudo_bin)},
    )
    assert "Backup verified successfully." in res.stdout


def test_rollback_priv_aware_path_exists_and_ignores_malformed_excluded_paths(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("Non-root EACCES privilege-aware check only applies when running as non-root")

    repo_dir, bootstrap_dir, _storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)
    apps_dir = repo_dir / "apps"

    # Seed excluded model file inside ollama/models plus an included key
    ollama_dir = apps_dir / "ollama"
    ollama_model = ollama_dir / "models" / "blobs" / "sha256-restricted"
    ollama_model.parent.mkdir(parents=True, exist_ok=True)
    ollama_model.write_bytes(b"restricted-model-weights")
    (ollama_dir / "id_ed25519").write_text("OLLAMA_KEY", encoding="utf-8")

    snap_dir = backup_parent / "priv-snap"
    _run(
        [
            "bash",
            str(bootstrap_dir / "backup.sh"),
            "--repo-dir",
            str(repo_dir),
            "--backup-dir",
            str(snap_dir),
            "--skip-docker",
        ]
    )

    # Inject malformed traversal / absolute excluded paths into metadata.env and re-sign SHA256SUMS
    outside_sentinel = tmp_path / "outside-sentinel"
    outside_sentinel.mkdir()
    (outside_sentinel / "keep.txt").write_text("untouched", encoding="utf-8")
    meta_path = snap_dir / "metadata.env"
    meta_text = meta_path.read_text(encoding="utf-8")
    meta_lines = []
    for line in meta_text.splitlines():
        if line.startswith("APPS_EXCLUDED_PATHS="):
            meta_lines.append(
                "APPS_EXCLUDED_PATHS=./home-server-dashboard|./ollama/models|/etc|../outside-sentinel|./foo/../bar"
            )
        else:
            meta_lines.append(line)
    meta_path.write_text("\n".join(meta_lines) + "\n", encoding="utf-8")
    meta_path.chmod(0o600)

    # Recompute SHA256SUMS
    meta_info = _parse_kv_file(meta_path)
    checksum_lines: list[str] = []
    for fname in (".env", "metadata.env", meta_info["APPS_ARCHIVE"], meta_info.get("MAINTAINERR_ARCHIVE", "")):
        if not fname:
            continue
        fpath = snap_dir / fname
        if fpath.is_file():
            checksum_lines.append(f"{hashlib.sha256(fpath.read_bytes()).hexdigest()}  {fname}")
    (snap_dir / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

    # Simulate a non-searchable root-owned directory (mode 0700 root:root) where unprivileged
    # bash `[[ -e ... || -L ... ]]` returns false due to EACCES while `sudo test -e` succeeds.
    locked_dir = apps_dir / "ollama"
    restricted_calls_log = tmp_path / "sudo-priv-calls.log"
    fake_bin = tmp_path / "priv-fake-bin"
    fake_bin.mkdir()

    # Locking `ollama` to mode 0000 causes EACCES on unprivileged path lookup even for the
    # owner. Our mock `sudo` temporarily grants 0755 on locked_dir during the privileged
    # command (simulating root bypassing DAC permissions) and restores 0000 afterwards.
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{restricted_calls_log}"
if [[ "${{1:-}}" == "-v" || "${{1:-}}" == "-n" ]]; then
  exit 0
fi
relock_paths=()
for p in "{apps_dir}/ollama" "{tmp_path}/HomeLab/.homelab-restore-stage."*/ollama; do
  if [[ -d "$p" ]]; then
    chmod 0755 "$p"
    relock_paths+=("$p")
  fi
done
rc=0
"$@" || rc=$?
for p in "${{relock_paths[@]}}"; do
  if [[ -d "$p" ]]; then
    chmod 0000 "$p"
  fi
done
exit "$rc"
""",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)

    locked_dir.chmod(0o000)
    try:
        rb = _run(
            [
                "bash",
                str(bootstrap_dir / "rollback.sh"),
                "--repo-dir",
                str(repo_dir),
                "--backup",
                str(snap_dir),
                "--skip-docker",
                "--yes",
            ],
            env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        )
        assert "Rollback completed successfully" in rb.stdout
    finally:
        if locked_dir.exists():
            locked_dir.chmod(0o755)

    sudo_log_lines = restricted_calls_log.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("test -e ") and "ollama/models" in line for line in sudo_log_lines)
    assert ollama_model.read_bytes() == b"restricted-model-weights"
    assert (outside_sentinel / "keep.txt").read_text(encoding="utf-8") == "untouched"


def test_backup_dry_run_root_notice_and_upgrade_nonfatal_sudo_preauth(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("Non-root sudo behavior test only applies when running as non-root")

    repo_dir, bootstrap_dir, _storage_dir, backup_parent = _seed_legacy_repo_clone(tmp_path)

    # 1. backup.sh --dry-run with restricted dir prints upfront notice before calling sudo du
    restricted_dir = repo_dir / "apps" / "plex" / "config" / "locked-sub"
    restricted_dir.mkdir(parents=True, exist_ok=True)
    restricted_dir.chmod(0o000)

    fake_bin = tmp_path / "dry-sudo-bin"
    fake_bin.mkdir()
    real_du = shutil.which("du")
    assert real_du is not None
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(
        f"""#!/usr/bin/env bash
if [[ "${{1:-}}" == "-v" ]]; then
  exit 0
fi
if [[ "${{1:-}}" == "du" ]]; then
  chmod 0755 "{restricted_dir}"
  rc=0
  "{real_du}" "${{@:2}}" || rc=$?
  chmod 0000 "{restricted_dir}"
  exit "$rc"
fi
exec "$@"
""",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)

    try:
        dry = _run(
            [
                "bash",
                str(bootstrap_dir / "backup.sh"),
                "--repo-dir",
                str(repo_dir),
                "--backup-dir",
                str(backup_parent / "dry-notice"),
                "--skip-docker",
                "--dry-run",
            ],
            env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
        )
        assert "Root privileges required for restricted service directories" in dry.stdout
        assert "[dry-run] Needs sudo:       true" in dry.stdout
    finally:
        if restricted_dir.exists():
            restricted_dir.chmod(0o755)

    # 2. upgrade.sh continues when upfront `sudo -v` fails under a TTY for a user who owns all files
    import pty

    upg_bin = tmp_path / "tty-upgrade-bin"
    upg_bin.mkdir()
    (upg_bin / "docker").write_text(
        """#!/usr/bin/env bash
if [[ "$1" == "compose" ]]; then
  shift
  case "$1" in
    config)
      if [[ "$*" == *"--services"* ]]; then
        printf 'plex\\n'
      fi
      ;;
    ps)
      if [[ "$*" == *"-q"* ]]; then
        printf 'deadbeef1234567890\\n'
      fi
      ;;
  esac
elif [[ "$1" == "inspect" ]]; then
  if [[ "$3" == *"State.Status"* ]]; then
    printf 'running\\n'
  elif [[ "$3" == *"State.Health"* ]]; then
    printf 'healthy\\n'
  elif [[ "$3" == *"State.Running"* ]]; then
    printf 'false\\n'
  fi
fi
exit 0
""",
        encoding="utf-8",
    )
    (upg_bin / "docker").chmod(0o755)
    # sudo fails -v (simulating non-sudoer), and succeeds no-op for fix-permissions if UID != 1000
    (upg_bin / "sudo").write_text(
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-v" ]]; then
  exit 1
fi
exit 0
""",
        encoding="utf-8",
    )
    (upg_bin / "sudo").chmod(0o755)

    master_fd, slave_fd = pty.openpty()
    output_chunks: list[bytes] = []
    try:
        proc = subprocess.Popen(
            [
                "bash",
                str(bootstrap_dir / "upgrade.sh"),
                "--repo-dir",
                str(repo_dir),
                "--target",
                "target-release",
                "--skip-fetch",
                "--no-pull",
                "--timeout",
                "20",
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env={
                **os.environ,
                "HOMELAB_BACKUP_DIR": str(backup_parent),
                "PATH": f"{upg_bin}:{os.environ['PATH']}",
            },
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1

        import select

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            rlist, _, _ = select.select([master_fd], [], [], 0.2)
            if rlist:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                output_chunks.append(data)
            elif proc.poll() is not None:
                break
        rc = proc.wait(timeout=10)
    finally:
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)

    pty_output = b"".join(output_chunks).decode("utf-8", errors="replace")
    assert rc == 0, f"upgrade.sh failed with rc={rc}:\n{pty_output}"
    assert "Note: sudo credentials not cached; continuing without pre-authentication" in pty_output
    assert "Upgrade complete!" in pty_output
