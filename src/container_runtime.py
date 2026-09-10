"""Prepare the mounted cache directory, then run the server as appuser."""

import os
import sys
from pathlib import Path


def main():
    cache = Path(os.environ.get("H1B_DATA_CACHE_DIR", "/app/data_cache"))
    temporary = cache / "tmp"
    cache.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(exist_ok=True)
    os.environ.setdefault("SQLITE_TMPDIR", str(temporary))
    if os.getuid() == 0:
        import pwd

        account = pwd.getpwnam("appuser")
        # setuid does not update the environment. Libraries resolving user
        # caches must never keep trying to access root's private home.
        os.environ["HOME"] = account.pw_dir
        os.environ.setdefault("XDG_CACHE_HOME", f"{account.pw_dir}/.cache")
        os.environ.setdefault("XDG_DATA_HOME", f"{account.pw_dir}/.local/share")
        # Existing pickles only need read access. Owning the parent permits
        # atomic index replacement without recursively rewriting the volume.
        for directory in (cache, temporary):
            os.chown(directory, account.pw_uid, account.pw_gid)
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    os.execv(
        sys.executable, [sys.executable, str(Path(__file__).with_name("server.py"))]
    )


if __name__ == "__main__":
    main()
