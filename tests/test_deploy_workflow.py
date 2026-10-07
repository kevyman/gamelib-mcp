"""The deploy workflow's host-side script, run for real against stub commands.

`.github/workflows/deploy.yml` carries a bash script that fetches, rebuilds and
rolls back on the production host. Its rollback rules exist to keep old code
from ever starting against a database a newer build migrated, and they are
exactly the kind of logic that is only exercised when a deploy goes wrong. The
tests here pull the script out of the workflow and run it with `git`, `docker`,
`sqlite3` and `sleep` replaced by stubs that log their calls and act out one
failure each.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="the deploy script is bash and only ever runs on the Linux host",
)

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy.yml"

_BUILD = "docker compose --profile prod build"
_UP = "docker compose --profile prod up -d"
_REBUILD = "docker compose --profile prod up -d --build"
_STOP_APP = "docker compose --profile prod stop app"
_START_APP = "docker compose --profile prod start app"
_ROLL_BACK = "git reset --hard previous-sha"

_STUBS = {
    "git": """
        case "$*" in
          "rev-parse HEAD") echo previous-sha ;;
          "rev-parse --short HEAD") echo new-sha ;;
        esac
    """,
    # `sqlite3 <db> 'PRAGMA user_version'`: the schema stamp is a file the
    # scenario moves, so the stub needs no SQLite.
    "sqlite3": """
        cat "$STUB_STATE/user_version"
    """,
    # `build` fails when the scenario says so. The first `up` starts the new
    # build: it runs the scenario's hook (what the new containers did) and
    # exits with the hook's status. `exec` is the /health probe.
    "docker": """
        case "$*" in
          "compose --profile prod build")
            if [ -f "$STUB_STATE/build_fails" ]; then
              exit 1
            fi
            ;;
          "compose --profile prod up -d"|"compose --profile prod up -d --build")
            if [ ! -f "$STUB_STATE/first_up_done" ]; then
              touch "$STUB_STATE/first_up_done"
              if [ -f "$STUB_STATE/on_first_up" ]; then
                . "$STUB_STATE/on_first_up"
              fi
            fi
            ;;
          "compose --profile prod exec "*)
            [ -f "$STUB_STATE/healthy" ] || exit 1
            ;;
        esac
    """,
    "sleep": "",
}


def _deploy_script() -> str:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    (step,) = [
        step
        for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("name") == "Deploy over SSH"
    ]
    return step["with"]["script"]


class _Host:
    """A fake deploy host: a clone directory, a database file, stubbed tools."""

    def __init__(self, root: Path) -> None:
        self.state = root / "state"
        self.bin = root / "bin"
        self.clone = root / "clone"
        self.db = self.clone / "data" / "library" / "gamelib.db"
        self.snapshot = Path(f"{self.db}.pre-v41.bak")
        self.marker = Path(f"{self.db}.migrating")
        for directory in (self.state, self.bin, self.db.parent):
            directory.mkdir(parents=True)
        self.db.write_text("v41 data")
        (self.state / "user_version").write_text("41\n")
        bash = shutil.which("bash")
        assert bash is not None
        self.bash = bash
        # An absolute interpreter path, so the stubs still run under a PATH
        # narrowed to the stub directory.
        for name, body in _STUBS.items():
            stub = self.bin / name
            stub.write_text(
                f"#!{bash}\n"
                f'echo "{name} $*" >> "$STUB_STATE/calls.log"\n'
                f"{body}\n",
                newline="\n",
            )
            stub.chmod(0o755)

    def on_first_up(self, shell: str) -> None:
        (self.state / "on_first_up").write_text(shell, newline="\n")

    def fail_build(self) -> None:
        (self.state / "build_fails").touch()

    def fresh(self) -> None:
        """A first deploy: no database on the host yet."""
        self.db.unlink()

    def mark_healthy(self) -> None:
        (self.state / "healthy").touch()

    def migrated_to_v42(self, *, snapshot: bool = True) -> str:
        """Shell for a new app that snapshotted, migrated and stamped v42."""
        lines = [
            f'echo "v42 data" > "{self.db.as_posix()}"',
            f'echo 42 > "{self.state.as_posix()}/user_version"',
        ]
        if snapshot:
            lines.insert(0, f'echo "v41 data" > "{self.snapshot.as_posix()}"')
        return "\n".join(lines) + "\n"

    def interrupted_mid_migration(self) -> str:
        """Shell for a new app that died mid-step: marker left, stamp unmoved."""
        return (
            f'echo "v41 data" > "{self.snapshot.as_posix()}"\n'
            f'echo "half migrated" > "{self.db.as_posix()}"\n'
            f'touch "{self.marker.as_posix()}"\n'
        )

    def deploy(
        self, *, path: str | None = None, deploy_path: Path | None = None
    ) -> tuple[int, list[str], str]:
        result = subprocess.run(
            [self.bash, "-c", _deploy_script()],
            env={
                "PATH": path or f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "HOME": self.clone.parent.as_posix(),
                "STUB_STATE": self.state.as_posix(),
                "DEPLOY_PATH": (deploy_path or self.clone).as_posix(),
                "GITHUB_TOKEN": "token",
                "GITHUB_REPOSITORY": "owner/repo",
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        log = self.state / "calls.log"
        calls = log.read_text().splitlines() if log.exists() else []
        return result.returncode, calls, result.stdout + result.stderr


@pytest.fixture
def host(tmp_path: Path) -> _Host:
    return _Host(tmp_path)


def test_healthy_deploy_keeps_the_new_build(host: _Host) -> None:
    host.mark_healthy()

    code, calls, output = host.deploy()

    assert code == 0, output
    assert _ROLL_BACK not in calls
    assert calls.index(_BUILD) < calls.index(_UP)
    assert calls.count(_UP) == 1
    assert _REBUILD not in calls
    assert "docker image prune -f" in calls


def test_compose_failure_before_anything_started_rolls_the_checkout_back(host: _Host) -> None:
    host.on_first_up("exit 1\n")

    code, calls, output = host.deploy()

    assert code == 1, output
    assert _ROLL_BACK in calls
    assert calls.count(_UP) == 1
    assert calls.count(_REBUILD) == 1
    assert host.db.read_text() == "v41 data"


def test_compose_failure_after_the_app_migrated_restores_the_snapshot(host: _Host) -> None:
    """Codex P1 on PR #202: `compose up` can fail AFTER the app started.

    Compose starts the app, which migrates on startup, and then returns
    nonzero for an unrelated service (Caddy losing its port). Rolling the
    checkout back without restoring the pre-migration snapshot starts the old
    build against a newer schema, which it refuses: the rollback becomes the
    outage.
    """
    host.on_first_up(host.migrated_to_v42() + "exit 1\n")

    code, calls, output = host.deploy()

    assert code == 1, output
    assert host.db.read_text().strip() == "v41 data", output
    assert _ROLL_BACK in calls
    # The app is stopped BEFORE the schema state is read (the second sqlite3
    # call; the first is the pre-deploy stamp): a container that only just
    # started must not begin migrating between the check and the rollback.
    schema_reads = [i for i, call in enumerate(calls) if call.startswith("sqlite3 ")]
    assert len(schema_reads) == 2, calls
    assert calls.index(_STOP_APP) < schema_reads[1] < calls.index(_ROLL_BACK)


def test_compose_failure_with_an_interrupted_migration_restores_the_snapshot(host: _Host) -> None:
    host.on_first_up(host.interrupted_mid_migration() + "exit 1\n")

    code, calls, output = host.deploy()

    assert code == 1, output
    assert host.db.read_text().strip() == "v41 data", output
    assert not host.marker.exists()
    assert _ROLL_BACK in calls


def test_compose_failure_without_a_snapshot_leaves_the_new_build_in_place(host: _Host) -> None:
    host.on_first_up(host.migrated_to_v42(snapshot=False) + "exit 1\n")

    code, calls, output = host.deploy()

    assert code == 1, output
    assert "NOT rolling back code" in output
    assert _ROLL_BACK not in calls
    assert host.db.read_text().strip() == "v42 data"
    # Stopped for the check, so it is started again rather than left down.
    assert calls.index(_STOP_APP) < calls.index(_START_APP)


def test_failed_health_gate_after_a_migration_restores_the_snapshot(host: _Host) -> None:
    host.on_first_up(host.migrated_to_v42())

    code, calls, output = host.deploy()

    assert code == 1, output
    assert host.db.read_text().strip() == "v41 data", output
    assert calls.index(_STOP_APP) < calls.index(_ROLL_BACK)
    assert calls.count(_UP) == 1
    assert calls.count(_REBUILD) == 1


def test_failed_health_gate_without_a_snapshot_does_not_roll_back(host: _Host) -> None:
    host.on_first_up(host.migrated_to_v42(snapshot=False))

    code, calls, output = host.deploy()

    assert code == 1, output
    assert "NOT rolling back code" in output
    assert _ROLL_BACK not in calls
    assert host.db.read_text().strip() == "v42 data"


def test_failed_health_gate_without_a_snapshot_restarts_the_app(host: _Host) -> None:
    """Stopped for the check, so the visible crash-loop is started again."""
    host.on_first_up(host.migrated_to_v42(snapshot=False))

    code, calls, output = host.deploy()

    assert code == 1, output
    assert calls.index(_STOP_APP) < calls.index(_START_APP)


def test_failed_health_gate_stops_the_app_before_reading_the_schema(host: _Host) -> None:
    """PR #202 review: a crash-looping container restarts on its own.

    Reading the schema while it may still come back up lets it migrate between
    the check and the rollback, so the gate path stops the app first, exactly
    like the compose-failure path.
    """
    host.on_first_up(host.migrated_to_v42())

    code, calls, output = host.deploy()

    assert code == 1, output
    schema_reads = [i for i, call in enumerate(calls) if call.startswith("sqlite3 ")]
    assert len(schema_reads) == 2, calls
    health_probes = [i for i, call in enumerate(calls) if " exec " in call]
    assert health_probes[-1] < calls.index(_STOP_APP) < schema_reads[1] < calls.index(_ROLL_BACK)
    assert host.db.read_text().strip() == "v41 data", output


def test_build_failure_rolls_the_checkout_back_without_touching_containers(host: _Host) -> None:
    """A pure image-build error must not cost the running build its uptime."""
    host.fail_build()

    code, calls, output = host.deploy()

    assert code == 1, output
    assert "docker compose build failed" in output
    assert _ROLL_BACK in calls
    docker_calls = [call for call in calls if call.startswith("docker ")]
    assert docker_calls == [_BUILD], calls
    assert host.db.read_text() == "v41 data"


def test_up_failure_after_a_successful_build_stops_and_inspects(host: _Host) -> None:
    host.on_first_up(host.migrated_to_v42() + "exit 1\n")

    code, calls, output = host.deploy()

    assert code == 1, output
    assert "docker compose up failed" in output
    assert (
        calls.index(_BUILD)
        < calls.index(_UP)
        < calls.index(_STOP_APP)
        < calls.index(_ROLL_BACK)
        < calls.index(_REBUILD)
    )
    assert host.db.read_text().strip() == "v41 data", output


def test_first_deploy_on_a_fresh_host_rolls_back_and_removes_the_new_database(
    host: _Host,
) -> None:
    """No database before the deploy means nothing to restore, not a stuck rollback.

    The app writes no pre-migration snapshot for a database it creates, so the
    rollback must not demand one: it removes what the new build created and the
    old build starts clean.
    """
    host.fresh()
    wal = Path(f"{host.db}-wal")
    shm = Path(f"{host.db}-shm")
    host.on_first_up(
        host.migrated_to_v42(snapshot=False)
        + f'touch "{wal.as_posix()}" "{shm.as_posix()}" "{host.marker.as_posix()}"\n'
    )

    code, calls, output = host.deploy()

    assert code == 1, output
    assert "NOT rolling back code" not in output
    assert calls.index(_STOP_APP) < calls.index(_ROLL_BACK) < calls.index(_REBUILD)
    for path in (host.db, wal, shm, host.marker):
        assert not path.exists(), path


def test_missing_sqlite3_with_an_existing_database_fails_before_fetching(host: _Host) -> None:
    """Without sqlite3 both schema reads are "unknown" and the gate never fires."""
    (host.bin / "sqlite3").unlink()

    code, calls, output = host.deploy(path=host.bin.as_posix())

    assert code == 1, output
    assert "::error::sqlite3 is required on the host for the schema gate" in output
    assert not any(call.startswith("git fetch") for call in calls), calls
    assert not any(call.startswith("docker ") for call in calls), calls


def test_missing_deploy_path_fails_with_an_annotation(host: _Host, tmp_path: Path) -> None:
    missing = tmp_path / "nowhere"

    code, calls, output = host.deploy(deploy_path=missing)

    assert code == 1, output
    assert f"::error::DEPLOY_PATH {missing.as_posix()} does not exist on the host" in output
    assert calls == []
