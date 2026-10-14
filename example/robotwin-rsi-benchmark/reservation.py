"""Durable port reservations. A crash deliberately leaves an operator-visible tombstone."""

import json
import os
from pathlib import Path
import socket


class WorkerReservation:
    def __init__(self, directory, run_root, ports):
        self.directory = Path(directory)
        self.run_root = Path(run_root).resolve()
        self.ports = sorted(set(ports))
        self.owned = []

    def acquire(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            for port in self.ports:
                marker = self.directory / f'port-{port}'
                try:
                    marker.mkdir()
                except FileExistsError as error:
                    raise RuntimeError(f'unresolved_worker_reservation: {marker}; '
                                       'reconcile its original run before reusing this port') from error
                self.owned.append(marker)
                (marker / 'owner.json').write_text(json.dumps({
                    'run_root': str(self.run_root), 'pid': os.getpid(),
                    'host': socket.gethostname(), 'port': port,
                    'automatic_reclaim': False}, indent=2))
        except BaseException:
            # No executor has been launched yet; releasing our partial acquisition is safe.
            self.release()
            raise

    def release(self):
        for marker in reversed(self.owned):
            (marker / 'owner.json').unlink(missing_ok=True)
            marker.rmdir()
        self.owned.clear()

    def release_after_cleanup(self, cleanup):
        if (cleanup.get('own_policy_exited') is not True
                or cleanup.get('skill_stop_error') is not None
                or cleanup.get('executor_shutdown_proven') is not True):
            raise RuntimeError('worker_cleanup_not_proven; reservation retained for manual reconciliation')
        self.release()
