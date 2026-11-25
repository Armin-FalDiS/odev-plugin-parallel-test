"""Check that the PostgreSQL server can take the databases of a parallel run, and measure what they use of it."""

import json
import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from odev.common.connectors import PostgresConnector


CONNECTIONS_PER_DATABASE = 4
"""Connections a test run holds at most: measured at 2, HttpCase tests open more for the requests they make."""

LOCK_MARGIN = 1.25
"""Room left on top of the lock slots the databases are expected to use, for the peaks not measured yet."""

LOCKED_SHARE_OF_RELATIONS = 0.6
"""Share of the tables and indexes of a database a test run locks at most at once (2916 of 4644 measured)."""

GIB = 1024**3


@dataclass
class ServerLimits:
    max_locks_per_transaction: int
    max_connections: int
    max_prepared_transactions: int
    superuser_reserved_connections: int
    shared_buffers: int
    """In bytes."""

    connections_in_use: int

    @property
    def lock_slots(self) -> int:
        """Size of the lock table, shared by every database of the server."""
        return self.max_locks_per_transaction * (self.max_connections + self.max_prepared_transactions)

    @property
    def free_connections(self) -> int:
        return self.max_connections - self.superuser_reserved_connections - self.connections_in_use


@dataclass
class Capacity:
    """How many test runs the server can take at once, and the settings it would need for more."""

    limits: ServerLimits
    locks_per_database: int

    @property
    def databases(self) -> int:
        by_locks = self.limits.lock_slots // math.ceil(self.locks_per_database * LOCK_MARGIN)
        by_connections = self.limits.free_connections // CONNECTIONS_PER_DATABASE
        return max(1, min(by_locks, by_connections))

    def settings_for(self, databases: int) -> dict[str, str]:
        """The settings to change for the server to take that many test runs at once."""
        settings = {}
        connections = self.limits.max_connections + self.limits.max_prepared_transactions
        locks = databases * self.locks_per_database * LOCK_MARGIN / connections

        if locks > self.limits.max_locks_per_transaction:
            settings["max_locks_per_transaction"] = str(max(256, 2 ** math.ceil(math.log2(locks))))

        if databases * CONNECTIONS_PER_DATABASE > self.limits.free_connections:
            needed = self.limits.max_connections - self.limits.free_connections + databases * CONNECTIONS_PER_DATABASE
            settings["max_connections"] = str(math.ceil(needed / 50) * 50)

        return settings


def read_limits() -> ServerLimits:
    with PostgresConnector() as psql, psql.nocache():
        rows = psql.query(
            """
            SELECT name, setting, unit FROM pg_settings
            WHERE name IN ('max_locks_per_transaction', 'max_connections', 'max_prepared_transactions',
                           'superuser_reserved_connections', 'shared_buffers')
            """
        )
        in_use = psql.query("SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'")

    settings = {name: int(setting) * unit_bytes(unit) for name, setting, unit in rows}  # type: ignore[union-attr]
    return ServerLimits(**settings, connections_in_use=in_use[0][0])  # type: ignore[index]


def unit_bytes(unit: str | None) -> int:
    """Bytes in a unit of pg_settings (`8kB` for shared_buffers); 1 for the settings that are plain numbers."""
    if not unit:
        return 1

    match = re.fullmatch(r"(\d*)(B|kB|MB|GB)", unit)

    if match is None:
        return 1

    multipliers = {"B": 1, "kB": 1024, "MB": 1024**2, "GB": GIB}
    return int(match.group(1) or 1) * multipliers[match.group(2)]


def estimate_locks(database: str) -> int:
    """The locks a test run on a database may hold, from its number of tables and indexes."""
    with PostgresConnector(database) as psql, psql.nocache():
        rows = psql.query(
            "SELECT count(*) FROM pg_class WHERE relkind IN ('r', 'p', 'i') AND relnamespace = 'public'::regnamespace"
        )

    return max(1, round(rows[0][0] * LOCKED_SHARE_OF_RELATIONS))  # type: ignore[index]


def locks_held(psql: PostgresConnector, databases: Sequence[str]) -> dict[str, int]:
    """Locks each database holds in the shared lock table; fast-path locks are kept by each backend instead."""
    rows = psql.query(
        """
        SELECT d.datname, count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database
        WHERE NOT l.fastpath AND d.datname = ANY(%s)
        GROUP BY d.datname
        """,
        [list(databases)],
    )
    return dict(rows or [])  # type: ignore[arg-type]


def recommended_shared_buffers() -> int:
    """An eighth of the memory, between 1 and 8 GiB."""
    memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return min(8, max(1, memory // 8 // GIB)) * GIB


def fix_commands(settings: dict[str, str]) -> list[str]:
    return [
        *(f'sudo -u postgres psql -c "ALTER SYSTEM SET {name} = \'{value}\'"' for name, value in settings.items()),
        "sudo systemctl restart postgresql",
    ]


class LockPeaks:
    """The most locks a single test run held, per source database, measured in earlier runs."""

    def __init__(self, path: Path):
        self.path = path

        try:
            self.peaks: dict[str, int] = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.peaks = {}

    def get(self, database: str) -> int | None:
        return self.peaks.get(database)

    def record(self, database: str, peak: int):
        if peak > self.peaks.get(database, 0):
            self.peaks[database] = peak
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.peaks, indent=2, sort_keys=True), encoding="utf-8")
