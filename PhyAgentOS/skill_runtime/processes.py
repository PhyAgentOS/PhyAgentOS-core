"""Linux process ownership for a managed runtime, including detached children.

This is cooperative lifecycle tracking, not a sandbox: nodes must preserve the
runtime marker in child environments. A dedicated cgroup is needed for stronger
containment. Never infer ownership from a command name or a reused PID.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

RUNTIME_MARKER = "PAOS_RUNTIME_INSTANCE_ID"


class RuntimeProcessTracker:
    def __init__(self, state_root: Path, instance_id: str) -> None:
        if not instance_id or not instance_id.replace("_", "").replace("-", "").isalnum():
            raise ValueError("invalid runtime process identity")
        self.instance_id = instance_id
        self.path = state_root / ".processes" / f"{instance_id}.json"
        self.supported = sys.platform.startswith("linux")
        self.known: dict[int, str] = {}
        self.boot_id = ""
        if self.supported:
            self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if self.path.exists():
                saved = json.loads(self.path.read_text())
                if saved["boot_id"] == self.boot_id:
                    self.known = {int(pid): start for pid, start in saved["processes"].items()}

    @staticmethod
    def _identity(pid: int) -> str | None:
        try:
            # comm may contain spaces and parentheses; fields after its last ')'
            # start at state (field 3), so starttime (field 22) is offset 19.
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return None if fields[0] in {"Z", "X"} else fields[19]
        except (FileNotFoundError, ProcessLookupError):
            return None

    def snapshot(self) -> dict[int, str]:
        if not self.supported:
            return {}
        marker = f"{RUNTIME_MARKER}={self.instance_id}".encode()
        found = {pid: start for pid, start in self.known.items() if self._identity(pid) == start}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != os.geteuid():
                    continue
                pid = int(entry.name)
                before = self._identity(pid)
                if before is not None and marker in (entry / "environ").read_bytes().split(b"\0"):
                    if self._identity(pid) == before:
                        found[pid] = before
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                # Unrelated non-dumpable processes need not expose their env.
                # Known owned processes remain covered by their start identity.
                continue
        self.known = found
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".processes-")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"boot_id": self.boot_id, "processes": found}, handle)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return found

    def _signal(self, pid: int, start: str, signum: int) -> None:
        # pidfd pins the target across PID recycling between check and kill, but
        # some Python builds (notably Anaconda) compile it out. Fall back to a
        # plain kill instead of failing the whole cleanup.
        open_pidfd = getattr(os, "pidfd_open", None)
        send_pidfd = getattr(signal, "pidfd_send_signal", None)
        if open_pidfd is not None and send_pidfd is not None:
            try:
                fd = open_pidfd(pid)
                try:
                    if self._identity(pid) == start:
                        send_pidfd(fd, signum)
                finally:
                    os.close(fd)
            except ProcessLookupError:
                return
            except OSError as exc:
                if exc.errno not in {errno.ENOSYS, errno.EOPNOTSUPP}:
                    raise
            else:
                return
        # Without pidfd the start-time re-check is the only guard; it narrows
        # the reuse window but cannot close it.
        try:
            if self._identity(pid) == start:
                os.kill(pid, signum)
        except ProcessLookupError:
            pass

    def terminate(self, *, timeout_s: float = 2.0) -> dict[int, str]:
        if not self.supported:
            return {}
        for signum in (signal.SIGTERM, signal.SIGKILL):
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = self.snapshot()
                if not remaining:
                    # A child can fork while /proc is being enumerated. Require
                    # another empty observation after its parent has exited.
                    time.sleep(0.05)
                    remaining = self.snapshot()
                    if not remaining:
                        return {}
                for pid, start in remaining.items():
                    self._signal(pid, start, signum)
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        return self.snapshot()
