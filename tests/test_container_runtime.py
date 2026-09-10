import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import container_runtime


def test_volume_ownership_is_repaired_before_dropping_privileges(tmp_path, monkeypatch):
    events = []
    monkeypatch.setenv("H1B_DATA_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("SQLITE_TMPDIR", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "pwd",
        SimpleNamespace(
            getpwnam=lambda name: SimpleNamespace(pw_uid=1234, pw_gid=5678)
        ),
    )
    monkeypatch.setattr(container_runtime.os, "getuid", lambda: 0, raising=False)
    for name in ("chown", "setgroups", "setgid", "setuid", "execv"):
        monkeypatch.setattr(
            container_runtime.os,
            name,
            lambda *args, name=name: events.append((name, args)),
            raising=False,
        )
    container_runtime.main()
    assert [event[0] for event in events] == [
        "chown",
        "chown",
        "setgroups",
        "setgid",
        "setuid",
        "execv",
    ]
    assert events[0][1] == (tmp_path, 1234, 5678)
    assert events[4][1] == (1234,)
    assert container_runtime.os.environ["SQLITE_TMPDIR"] == str(tmp_path / "tmp")
