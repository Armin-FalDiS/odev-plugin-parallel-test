"""Run the tests of a database in parallel on several copies of it."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from odev.common import args, string
from odev.common.commands import OdoobinCommand
from odev.common.commands.odoobin import TEMPLATE_SUFFIX
from odev.common.connectors import PostgresConnector
from odev.common.console import TableHeader
from odev.common.databases import LocalDatabase
from odev.common.logging import logging, silence_loggers
from odev.common.odoobin import OdoobinProcess
from odev.common.progress import Progress

from odev.plugins.odev_plugin_parallel_test.common import logs, postgres, sharding


logger = logging.getLogger(__name__)


DISCOVERY_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "discover_tests.py"
DISCOVERY_MARKER = "PARALLEL-TEST-DISCOVERY "
POLL_INTERVAL = 1.0
SHUTDOWN_TIMEOUT = 10
MAX_DETAIL_LINES = 40
"""Lines of a warning or error printed in the report; the rest is in the log file."""


@dataclass
class OdooRun:
    """An odoo-bin process running in the background and writing its output to a log file."""

    label: str
    database: str
    log_path: Path
    total: int
    """Progress to reach: the number of tests to run, or of modules to load for the base database."""

    counts_modules: bool = False
    process: subprocess.Popen | None = None
    started_at: float = 0.0
    ended_at: float = 0.0
    completed: int = 0
    _unread: str = ""
    _read_offset: int = 0

    @property
    def seconds(self) -> float:
        return (self.ended_at or time.monotonic()) - self.started_at

    def update_progress(self):
        for line in self._read_new_lines():
            if self.counts_modules:
                if match := logs.MODULE_LOADING_LINE.search(line):
                    self.completed, self.total = int(match.group("current")), int(match.group("total"))
            elif logs.TEST_START_LINE.search(line):
                self.completed += 1

    def _read_new_lines(self) -> list[str]:
        with self.log_path.open(encoding="utf-8", errors="replace") as log_file:
            log_file.seek(self._read_offset)
            chunk = log_file.read()
            self._read_offset = log_file.tell()

        *lines, self._unread = (self._unread + chunk).split("\n")
        return lines


@dataclass
class RunResult:
    label: str
    log_path: Path
    seconds: float
    records: list[logs.LogRecord] = field(default_factory=list)
    counts: logs.TestCounts | None = None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def format_duration(seconds: float) -> str:
    minutes, seconds = divmod(round(seconds), 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


def signal_process_group(process: subprocess.Popen, signal_number: int):
    try:
        os.killpg(process.pid, signal_number)
    except ProcessLookupError:
        pass


class ParallelTestCommand(OdoobinCommand):
    """Run the tests of a database in parallel on several copies of it.

    The database is copied and the given modules are updated or installed on the copy, which runs their at_install
    tests. The copy is then cloned into several databases, each running a share of the remaining tests, and the
    results of all runs are pooled into one report. The copies are deleted afterwards.
    """

    _name = "ptest"
    _aliases = ["parallel-test"]

    tests = args.List(
        aliases=["-t", "--tags"],
        default=[],
        description="""Comma-separated list of test tags selecting the tests to run, as for odoo-bin --test-tags.
        Defaults to all the tests of the modules passed to --update and --init.
        """,
    )
    update = args.List(
        aliases=["-u", "--update"],
        default=[],
        description="Comma-separated list of modules to update on the copy of the database before testing.",
    )
    install = args.List(
        aliases=["-i", "--init"],
        default=[],
        description="Comma-separated list of modules to install on the copy of the database before testing.",
    )
    jobs = args.Integer(
        aliases=["-j", "--jobs"],
        description="""Number of databases running tests at the same time. Defaults to half the CPUs, lowered when
        more databases could not finish sooner because of the longest test class.
        """,
    )
    keep = args.Flag(
        aliases=["-k", "--keep"],
        description="Keep the databases created for the run instead of deleting them, to investigate failures.",
    )

    @property
    def _database_exists_required(self) -> bool:
        return not bool(self.args.version)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.run_id = string.suid()
        self.logs_path = Path(tempfile.gettempdir()) / "odev-parallel-test" / self.run_id
        self.base_database_name = f"{self._database.name.removesuffix(TEMPLATE_SUFFIX)}-ptest-{self.run_id}"
        self.created_databases: list[str] = []
        self.runs: list[OdooRun] = []
        self.timings = sharding.TimingStore(self.odev.home_path / "parallel-test" / "timings.json")
        self.lock_peaks = postgres.LockPeaks(self.odev.home_path / "parallel-test" / "lock-peaks.json")
        self.postgres_checked = False

    @property
    def test_tags(self) -> list[str]:
        return self.args.tests or [f"/{module}" for module in [*self.args.install, *self.args.update]]

    # --------------------------------------------------------------------------
    # Steps
    # --------------------------------------------------------------------------

    def run(self):
        if not self.test_tags:
            raise self.error("Nothing to test: pass the modules to test with --update or --init, or test tags with -t")

        if self.args.jobs and self._database.exists:
            self.check_postgres(self.args.jobs, explicit=True)

        self.logs_path.mkdir(parents=True)
        started_at = time.monotonic()

        self.create_base_database()
        base_result = self.prepare_base_database()
        base_tests = set(logs.started_tests(base_result.records)) if base_result else set()

        tests, class_sizes = self.discover_tests()
        units = sharding.group_by_class([test for test in tests if tuple(test[:3]) not in base_tests], class_sizes)
        shard_results: list[RunResult] = []

        if units:
            self.timings.estimate(units)
            shards = self.split(units)
            shard_runs = self.clone_base_database(shards)
            shard_results = self.run_in_parallel(shard_runs, [shard.test_tags for shard in shards])
            self.save_timings(shard_results)
        else:
            logger.info("No test left to run in parallel")

        results = [result for result in [base_result, *shard_results] if result is not None]
        wall_seconds = time.monotonic() - started_at
        # Deleted before the report, so that the report is the last thing on screen.
        self.delete_databases()
        self.report(results, wall_seconds, sum(len(unit.methods) for unit in units))

    def create_base_database(self):
        self.created_databases.append(self.base_database_name)

        if self._database.exists:
            create_args = ["--from-template", self._database.name]
        else:
            create_args = ["--bare", "--version", str(self.version)]

        if not self.odev.run_command("create", *create_args, self.base_database_name):
            raise self.error(f"Could not create the database {self.base_database_name!r}")

    def prepare_base_database(self) -> RunResult | None:
        """Update or install the modules on the base database, running their at_install tests on the way."""
        install = self.args.install if self._database.exists else [*self.args.install, *self.args.update]
        update = self.args.update if self._database.exists else []
        module_args = [
            *(["--init", ",".join(install)] if install else []),
            *(["--update", ",".join(update)] if update else []),
        ]

        if not module_args:
            return None

        process = self.odoobin_process(self.base_database_name)

        if not process.venv.exists:
            process.prepare_odoobin()

        base_run = OdooRun("base (at_install)", self.base_database_name, self.logs_path / "base.log", 0, True)
        # Excluding post_install keeps them for the shards: they would otherwise run here, after the update.
        result = self.run_in_parallel(
            [base_run],
            [",".join([*self.test_tags, "-post_install"])],
            module_args,
            # Updating modules locks far more than running tests; it would size the shards too small.
            measure_locks=False,
        )[0]

        if result.counts is None or any(record.level == "CRITICAL" for record in result.records):
            raise self.error(f"Preparing the base database failed, see {result.log_path}")

        return result

    def discover_tests(self) -> tuple[list[list], dict[str, int]]:
        """List the tests selected by the test tags, by asking Odoo itself on the base database."""
        script = f"TEST_TAGS = {json.dumps(','.join(self.test_tags))}\n{DISCOVERY_SCRIPT_PATH.read_text()}"
        process = self.odoobin_process(self.base_database_name)
        command = self.odoobin_command(process, ["--log-level", "warn"], subcommand="shell")
        log_path = self.logs_path / "discovery.log"

        logger.info("Listing the tests to run")
        started_at = time.monotonic()
        result = subprocess.run(command, input=script, capture_output=True, text=True, check=False)  # noqa: S603
        log_path.write_text(result.stderr + result.stdout, encoding="utf-8")
        output = next((line for line in result.stdout.splitlines() if line.startswith(DISCOVERY_MARKER)), None)

        if output is None:
            raise self.error(f"Could not list the tests to run, see {log_path}")

        discovery = json.loads(output.removeprefix(DISCOVERY_MARKER))
        # Mostly the assets bundles when the source database has none: a few seconds otherwise.
        logger.info(
            f"Listed {len(discovery['tests'])} tests and prepared the assets bundles "
            f"in {format_duration(time.monotonic() - started_at)}"
        )
        return discovery["tests"], discovery["class_sizes"]

    def split(self, units: list[sharding.WorkUnit]) -> list[sharding.Shard]:
        shard_count = self.args.jobs or sharding.default_shard_count(units, os.cpu_count() or 2)

        if not self.postgres_checked:
            shard_count = self.check_postgres(shard_count, explicit=bool(self.args.jobs))

        shards = sharding.split(units, shard_count)

        if too_long := [shard for shard in shards if len(shard.test_tags) > sharding.MAX_TEST_TAGS_LENGTH]:
            raise self.error(
                f"The test tags of {len(too_long)} shard(s) are too long for a command line, use more shards (-j)"
            )

        logger.info(
            f"Running {sum(shard.test_count for shard in shards)} tests of {len(units)} classes "
            f"on {len(shards)} databases in parallel:\n"
            + string.join_bullet(
                [
                    f"shard {shard.index}: {len(shard.units)} classes, {shard.test_count} tests, "
                    f"about {format_duration(shard.estimate)}"
                    for shard in shards
                ]
            )
        )
        return shards

    def check_postgres(self, shard_count: int, explicit: bool) -> int:
        """The number of databases to run, lowered to what PostgreSQL can take unless it was asked for explicitly.

        Too many at once and PostgreSQL runs out of lock slots, which makes tests fail at random.
        """
        self.postgres_checked = True
        capacity = postgres.Capacity(postgres.read_limits(), self.locks_per_database())
        self.suggest_shared_buffers(capacity.limits, shard_count)

        if shard_count <= capacity.databases:
            return shard_count

        commands = string.join_bullet(postgres.fix_commands(capacity.settings_for(shard_count)))

        if not explicit:
            logger.warning(
                f"PostgreSQL can take {capacity.databases} test databases at once, running that many instead of "
                f"{shard_count}. To run {shard_count}, run these commands first:\n{commands}"
            )
            return capacity.databases

        logger.warning(
            f"PostgreSQL can take {capacity.databases} test databases at once: with {shard_count}, it may run out "
            f"of lock slots or connections and fail tests at random. To fix it, run these commands first:\n{commands}"
        )

        if self.args.bypass_prompt:
            return shard_count

        if not sys.stdin.isatty() or not self.console.confirm(f"Run {shard_count} anyway?", default=False):
            raise self.error(f"Not running {shard_count} test databases at once; pass -f to run them anyway")

        return shard_count

    def locks_per_database(self) -> int:
        """The locks a test run holds at most: measured in earlier runs, or estimated from the size of the schema."""
        if peak := self.lock_peaks.get(self._database.name):
            return peak

        return postgres.estimate_locks(self._database.name if self._database.exists else self.base_database_name)

    def suggest_shared_buffers(self, limits: postgres.ServerLimits, shard_count: int):
        recommended = postgres.recommended_shared_buffers()

        if shard_count > 1 and limits.shared_buffers < postgres.GIB:
            commands = string.join_bullet(postgres.fix_commands({"shared_buffers": f"{recommended // postgres.GIB}GB"}))
            logger.info(
                f"PostgreSQL caches {limits.shared_buffers // 1024**2} MB of data (shared_buffers), too little for "
                f"several test databases at once. To give it {recommended // postgres.GIB} GB, run:\n{commands}"
            )

    def clone_base_database(self, shards: list[sharding.Shard]) -> list[OdooRun]:
        shard_runs = []

        with silence_loggers("odev"):
            for shard in shards:
                database = f"{self.base_database_name}-{shard.index}"
                self.created_databases.append(database)

                if not self.odev.run_command("create", "--from-template", self.base_database_name, database):
                    raise self.error(f"Could not create the database {database!r}")

                log_path = self.logs_path / f"shard-{shard.index}.log"
                shard_runs.append(OdooRun(f"shard {shard.index}", database, log_path, shard.test_count))

        return shard_runs

    def run_in_parallel(
        self,
        runs: list[OdooRun],
        test_tags: list[str],
        extra_args: list[str] | None = None,
        measure_locks: bool = True,
    ) -> list[RunResult]:
        """Run odoo-bin on each database with its test tags, showing their progress until they all stop.

        :param measure_locks: Record the most locks one of them held, to size the next runs on PostgreSQL.
        """
        for odoo_run, tags in zip(runs, test_tags, strict=True):
            self.start(odoo_run, tags, extra_args or [])

        lock_peak = 0

        with Progress() as progress, PostgresConnector() as psql, psql.nocache():
            tasks = {odoo_run.label: progress.add_task(odoo_run.label, total=odoo_run.total or None) for odoo_run in runs}

            while running := [odoo_run for odoo_run in runs if not odoo_run.ended_at]:
                if measure_locks:
                    locks = postgres.locks_held(psql, [odoo_run.database for odoo_run in running])
                    lock_peak = max(lock_peak, *locks.values(), 0)

                for odoo_run in running:
                    if odoo_run.process is not None and odoo_run.process.poll() is not None:
                        odoo_run.ended_at = time.monotonic()

                    odoo_run.update_progress()
                    progress.update(
                        tasks[odoo_run.label],
                        completed=odoo_run.completed,
                        total=odoo_run.total or None,
                    )

                time.sleep(POLL_INTERVAL)

        if measure_locks:
            self.lock_peaks.record(self._database.name, lock_peak)
        results = [self.result(odoo_run) for odoo_run in runs]

        for result in results:
            outcome = (
                f"{result.counts.run} tests, {result.counts.failed + result.counts.errors} failing"
                if result.counts
                else "stopped before the end of the tests"
            )
            logger.info(f"{result.label.capitalize()} done in {format_duration(result.seconds)}: {outcome}")

        return results

    def start(self, odoo_run: OdooRun, test_tags: str, extra_args: list[str]):
        odoo_args = [
            "--stop-after-init",
            "--test-enable",
            "--test-tags",
            test_tags,
            "--http-port",
            str(free_port()),
            "--max-cron-threads",
            "0",
            "--log-level",
            "info",
            # The detailed report gives the duration of each test, to measure the classes for the next runs.
            "--log-handler",
            "odoo.tests.stats:DEBUG",
            *extra_args,
            *self.combined_odoo_args,
        ]
        command = self.odoobin_command(self.odoobin_process(odoo_run.database), odoo_args)

        with odoo_run.log_path.open("w", encoding="utf-8") as log_file:
            odoo_run.process = subprocess.Popen(  # noqa: S603
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                # Its own process group, so that stopping it also stops the browsers its tests started.
                start_new_session=True,
            )

        odoo_run.started_at = time.monotonic()
        self.runs.append(odoo_run)

    def result(self, odoo_run: OdooRun) -> RunResult:
        records = list(logs.read_records(odoo_run.log_path))
        return RunResult(odoo_run.label, odoo_run.log_path, odoo_run.seconds, records, logs.count_tests(records))

    def save_timings(self, results: list[RunResult]):
        for result in results:
            tests_per_class = Counter(test[:2] for test in logs.started_tests(result.records))

            for (module, test_class), seconds in logs.class_durations(result.records).items():
                self.timings.record(f"{module}:{test_class}", seconds, tests_per_class[module, test_class])

        self.timings.save()

    # --------------------------------------------------------------------------
    # Report
    # --------------------------------------------------------------------------

    def report(self, results: list[RunResult], wall_seconds: float, expected_shard_tests: int):
        self.print_failures(results)
        self.print_problems(results)
        self.print_summary(results, wall_seconds)

        crashed = [result.label for result in results if result.counts is None]
        total = sum((result.counts for result in results if result.counts), logs.TestCounts())
        shard_tests = sum(result.counts.run for result in results if result.counts and result.label.startswith("shard"))

        if not crashed and shard_tests < expected_shard_tests:
            logger.warning(f"{expected_shard_tests} tests were listed for the shards but only {shard_tests} ran")

        logger.info(f"Logs are in {self.logs_path}")

        if any("max_locks_per_transaction" in record.text for result in results for record in result.records):
            logger.warning(
                "PostgreSQL ran out of lock slots: they are shared by all the databases running at once, and each "
                "test transaction holds many locks. Run fewer databases (-j) or raise max_locks_per_transaction "
                "in postgresql.conf (1024 is plenty), then restart PostgreSQL"
            )

        if crashed:
            raise self.error(f"Odoo stopped before the end of the tests in {string.join_and(crashed)}")

        if total.failed or total.errors:
            raise self.error(f"{total.failed} failed and {total.errors} error(s) out of {total.run} tests")

        logger.info(f"All {total.run} tests passed")

    def print_failures(self, results: list[RunResult]):
        failures = [(result, record) for result in results for record in result.records if logs.is_failure(record)]

        if not failures:
            return

        self.print()
        self.print(string.stylize(f"Failed tests ({len(failures)})", "bold color.red"))

        for result, record in failures:
            self.print()
            self.print(f"{record.message}  [{record.test_module}, {result.label}]", style="bold color.red", highlight=False)
            self.print(string.indent("\n".join(record.details).rstrip(), 2), style="color.red", highlight=False)

    def print_problems(self, results: list[RunResult]):
        """Print the warnings and errors that are not test failures, each one once whatever the runs it appeared in."""
        occurrences: dict[tuple[str, str, str], list[str]] = {}
        first_record: dict[tuple[str, str, str], logs.LogRecord] = {}

        for result in results:
            for record in filter(logs.is_problem, result.records):
                key = (record.level, record.logger, record.text_without_database)
                occurrences.setdefault(key, []).append(result.label)
                first_record.setdefault(key, record)

        if not occurrences:
            return

        self.print()
        self.print(string.stylize(f"Warnings and errors ({len(occurrences)})", "bold color.yellow"))

        for key, labels in occurrences.items():
            record = first_record[key]
            seen_in = labels[0] if len(labels) == 1 else f"{len(labels)} times: {string.join_and(sorted(set(labels)))}"
            details = record.details[:MAX_DETAIL_LINES]

            if len(record.details) > MAX_DETAIL_LINES:
                details.append(f"... {len(record.details) - MAX_DETAIL_LINES} more lines in the log")

            self.print()
            self.print(
                f"{record.level} {record.logger}: {record.message}  [{seen_in}]",
                style=f"logging.level.{record.level.lower()}",
                highlight=False,
            )

            if details:
                self.print(string.indent("\n".join(details).rstrip(), 2), style="color.black", highlight=False)

    def print_summary(self, results: list[RunResult], wall_seconds: float):
        rows = []

        for result in results:
            if counts := result.counts:
                numbers = [counts.run, counts.passed, counts.failed, counts.errors, counts.skipped]
            else:
                numbers = ["crashed", "", "", "", ""]
            rows.append([result.label, *map(str, numbers), format_duration(result.seconds)])

        total = sum((result.counts for result in results if result.counts), logs.TestCounts())
        self.print()
        self.table(
            [
                TableHeader("Run"),
                TableHeader("Tests", align="right"),
                TableHeader("Passed", align="right", style="color.green"),
                TableHeader("Failed", align="right", style="color.red"),
                TableHeader("Errors", align="right", style="color.red"),
                TableHeader("Skipped", align="right"),
                TableHeader("Duration", align="right"),
            ],
            rows,
            totals=[
                "Total",
                *map(str, [total.run, total.passed, total.failed, total.errors, total.skipped]),
                format_duration(wall_seconds),
            ],
        )
        logger.info(
            f"Ran in {format_duration(wall_seconds)}, setup included; the same runs one after the other "
            f"would take about {format_duration(sum(result.seconds for result in results))}, setup excluded"
        )

    # --------------------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------------------

    def odoobin_process(self, database: str) -> OdoobinProcess:
        process = self.odev.odoobin_process_class(
            database=LocalDatabase(database),
            version=self.version,
            venv=self.venv.name,
            worktree=self.worktree,
        ).with_edition("enterprise" if self.args.enterprise else "community")
        process.additional_addons_paths = self.odoobin.additional_addons_paths
        return process

    @staticmethod
    def odoobin_command(process: OdoobinProcess, odoo_args: list[str], subcommand: str | None = None) -> list[str]:
        return [str(process.venv.python), str(process.odoobin_path), *process.prepare_odoobin_args(odoo_args, subcommand)]

    def cleanup(self):
        self.stop_runs()
        self.delete_databases()

    def delete_databases(self):
        if self.args.keep:
            if self.created_databases:
                logger.info(f"Kept the databases {string.join_and([repr(name) for name in self.created_databases])}")
            self.created_databases.clear()
            return

        with silence_loggers("odev"):
            for name in reversed(self.created_databases):
                database = LocalDatabase(name)

                if database.exists:
                    database.whitelisted = False
                    self.odev.run_command("delete", "--force", "--keep", "venv", name)

        if self.created_databases:
            logger.info(f"Deleted the {len(self.created_databases)} databases created for the run")

        self.created_databases.clear()

    def stop_runs(self):
        running = [odoo_run.process for odoo_run in self.runs if odoo_run.process and odoo_run.process.poll() is None]

        for process in running:
            signal_process_group(process, signal.SIGTERM)

        for process in running:
            try:
                process.wait(timeout=SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                signal_process_group(process, signal.SIGKILL)
                process.wait()
