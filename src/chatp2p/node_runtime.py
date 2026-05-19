"""Managed background process helpers for local ChatP2P nodes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_SCHEMA = "chatp2p.managed-process.v1"
MANAGED_ROLES = ("coordinator", "worker")
SECRET_FLAGS = {"--admission-token"}


def default_coordinator_url(host: str, port: int) -> str:
    """Return the URL a local worker should use for a bound coordinator."""
    worker_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    if ":" in worker_host and not worker_host.startswith("["):
        worker_host = f"[{worker_host}]"
    return f"http://{worker_host}:{port}"


def managed_process_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    source_root = str(Path(__file__).resolve().parents[1])
    python_path = env.get("PYTHONPATH")
    paths = [] if not python_path else python_path.split(os.pathsep)
    if source_root not in paths:
        paths.insert(0, source_root)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def redact_command_args(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        if arg in SECRET_FLAGS:
            redacted.append(arg)
            skip_next = True
            continue
        if any(arg.startswith(f"{flag}=") for flag in SECRET_FLAGS):
            flag, _, _value = arg.partition("=")
            redacted.append(f"{flag}=<redacted>")
            continue
        redacted.append(arg)
    return redacted


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def start_managed_process(
    *,
    home: Path,
    role: str,
    argv: list[str],
    coordinator_url: str | None = None,
    cwd: Path | None = None,
    force: bool = False,
    extra_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if role not in MANAGED_ROLES:
        raise ValueError(f"unsupported managed role: {role}")

    home = home.expanduser().resolve()
    run_dir = _run_dir(home)
    log_dir = _log_dir(home)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    existing = read_process_state(home, role)
    if existing and process_alive(int(existing.get("pid", 0))):
        if not force:
            return {"role": role, "status": "already_running", "state": _state_with_alive(existing)}
        stop_managed_process(home=home, role=role)

    stdout_path = log_dir / f"{role}.out.log"
    stderr_path = log_dir / f"{role}.err.log"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process_cwd = str(cwd.resolve() if cwd else Path.cwd().resolve())
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        process = subprocess.Popen(
            argv,
            cwd=process_cwd,
            env=managed_process_env(),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )

    state: dict[str, Any] = {
        "schema": STATE_SCHEMA,
        "role": role,
        "pid": process.pid,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "home": str(home),
        "cwd": process_cwd,
        "command": redact_command_args(argv),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }
    if coordinator_url is not None:
        state["coordinator"] = coordinator_url
    if extra_state:
        state.update(extra_state)

    _write_process_state(home, role, state)
    return {"role": role, "status": "started", "state": _state_with_alive(state)}


def stop_managed_process(*, home: Path, role: str, timeout_seconds: float = 5.0) -> dict[str, Any]:
    if role not in MANAGED_ROLES:
        raise ValueError(f"unsupported managed role: {role}")

    home = home.expanduser().resolve()
    state = read_process_state(home, role)
    if not state:
        return {"role": role, "status": "not_managed", "state_path": str(_state_path(home, role))}

    pid = int(state.get("pid", 0))
    if process_alive(pid):
        terminate_process(pid)
        if not wait_for_process_exit(pid, timeout_seconds):
            kill_process(pid)
            wait_for_process_exit(pid, 1.0)

    alive = process_alive(pid)
    if not alive:
        _state_path(home, role).unlink(missing_ok=True)
    return {
        "role": role,
        "status": "stopped" if not alive else "stop_timeout",
        "pid": pid,
        "alive": alive,
        "state_path": str(_state_path(home, role)),
    }


def managed_process_status(*, home: Path, role: str) -> dict[str, Any]:
    if role not in MANAGED_ROLES:
        raise ValueError(f"unsupported managed role: {role}")
    home = home.expanduser().resolve()
    state = read_process_state(home, role)
    if not state:
        return {
            "role": role,
            "managed": False,
            "alive": False,
            "state_path": str(_state_path(home, role)),
        }
    report = _state_with_alive(state)
    report["managed"] = True
    report["state_path"] = str(_state_path(home, role))
    return report


def managed_processes_status(*, home: Path, roles: tuple[str, ...] = MANAGED_ROLES) -> list[dict[str, Any]]:
    return [managed_process_status(home=home, role=role) for role in roles]


def read_process_state(home: Path, role: str) -> dict[str, Any] | None:
    path = _state_path(home.expanduser().resolve(), role)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def wait_for_process_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.time() + max(timeout_seconds, 0)
    while time.time() <= deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.05)
    return not process_alive(pid)


def terminate_process(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        _windows_terminate_process(pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


def kill_process(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        _windows_terminate_process(pid)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def _state_with_alive(state: dict[str, Any]) -> dict[str, Any]:
    report = dict(state)
    report["alive"] = process_alive(int(state.get("pid", 0)))
    return report


def _write_process_state(home: Path, role: str, state: dict[str, Any]) -> None:
    _state_path(home, role).write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _run_dir(home: Path) -> Path:
    return home / "run"


def _log_dir(home: Path) -> Path:
    return home / "logs"


def _state_path(home: Path, role: str) -> Path:
    return _run_dir(home) / f"{role}.pid.json"


def _windows_process_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    still_active = 259
    exit_code = ctypes.c_ulong()
    for access in (0x1000, 0x0400):
        handle = kernel32.OpenProcess(access, False, int(pid))
        if not handle:
            continue
        try:
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    return False


def _windows_terminate_process(pid: int) -> None:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0001, False, int(pid))
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)
