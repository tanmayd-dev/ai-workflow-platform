#!/usr/bin/env python3
"""Container entrypoint.

Prepares the mounted data volumes (directories, ownership, permissions), runs
Alembic migrations, then drops privileges and execs the container command.
This keeps a fresh named volume usable without any manual setup.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

# The app package lives at the image WORKDIR (/app); make it importable even
# when this script is executed from /usr/local/bin.
sys.path.insert(0, os.getcwd())

from app.core.config import (  # noqa: E402  (path set up above)
    DEFAULT_CHROMA_DB_DIR,
    DEFAULT_DATABASE_URL,
    DEFAULT_DOCUMENTS_DIR,
)

DIR_MODE = 0o775


def _app_ids() -> tuple[int, int]:
    """UID/GID the application process runs as."""
    return (
        int(os.environ.get("APP_UID", "10001")),
        int(os.environ.get("APP_GID", "10001")),
    )


def _data_directories() -> list[Path]:
    """Directories that must exist and be writable by the app user."""
    directories = [
        Path(os.environ.get("CHROMA_DB_DIR", DEFAULT_CHROMA_DB_DIR)),
        Path(os.environ.get("DOCUMENTS_DIR", DEFAULT_DOCUMENTS_DIR)),
    ]

    database_url = make_url(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    if (
        database_url.get_backend_name() == "sqlite"
        and database_url.database
        and database_url.database != ":memory:"
    ):
        parent = Path(database_url.database).expanduser().parent
        # Skip the working directory itself for local relative DB paths.
        if parent not in (Path("."), Path("")):
            directories.append(parent)

    # De-duplicate while preserving order.
    unique: list[Path] = []
    for directory in directories:
        if directory not in unique:
            unique.append(directory)
    return unique


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    """Recursively give path and its contents to the app user."""
    for root, dirnames, filenames in os.walk(path):
        os.chown(root, uid, gid)
        for name in dirnames + filenames:
            os.chown(os.path.join(root, name), uid, gid, follow_symlinks=False)


def prepare_storage() -> bool:
    """Create data directories and set ownership/permissions.

    Returns True when the directories are usable by the app user. When running
    rootless (e.g. rootless Podman) chown can be refused by the kernel; in that
    case we log a warning and keep the current user instead of restarting.
    """
    uid, gid = _app_ids()
    is_root = os.geteuid() == 0
    app_owns_storage = True

    for directory in _data_directories():
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, DIR_MODE)
        if is_root:
            try:
                _chown_tree(directory, uid, gid)
            except PermissionError as exc:
                app_owns_storage = False
                print(
                    f"[entrypoint] warning: cannot chown {directory} to "
                    f"{uid}:{gid} ({exc}); continuing as the current user",
                    file=sys.stderr,
                )
        print(f"[entrypoint] storage ready: {directory}")

    return app_owns_storage


def drop_privileges(owns_storage: bool) -> None:
    """Drop from root to APP_UID/APP_GID before running the app."""
    if os.geteuid() != 0 or not owns_storage:
        return
    uid, gid = _app_ids()
    if uid == 0:
        return
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    print(f"[entrypoint] running as uid={uid} gid={gid}")


def run_migrations() -> None:
    """Converge the schema on the current Alembic head."""
    print("[entrypoint] running: alembic upgrade head")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
    )


def main() -> None:
    owns_storage = prepare_storage()
    drop_privileges(owns_storage)
    run_migrations()

    if len(sys.argv) < 2:
        sys.exit("[entrypoint] no command supplied")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
