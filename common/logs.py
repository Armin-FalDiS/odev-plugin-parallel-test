"""Read the log files written by odoo-bin during a test run."""

import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path


LOG_LINE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \d+ "
    r"(?P<level>[A-Z]+) (?P<database>\S+) (?P<logger>\S+): (?P<message>.*)$"
)
TEST_LOGGER = re.compile(r"^odoo\.addons\.(?P<module>\w+)\.tests\.")
TEST_START = re.compile(r"^Starting (?P<test_class>[\w.]+)\.(?P<method>\w+) \.\.\.$")
TEST_SKIP = re.compile(r"^skipped [\w.]+\.\w+ : ")
TEST_FAILURE = re.compile(r"^(?P<status>FAIL|ERROR): (?P<test>.+)$")
RESULT = re.compile(r"^(?P<failed>\d+) failed, (?P<errors>\d+) error\(s\) of (?P<run>\d+) tests when loading database")
TEST_STAT = re.compile(
    r"^\s*odoo\.addons\.(?P<module>\w+)\.tests\.[\w.]+\.(?P<test_class>\w+)\.(?P<method>\w+): (?P<seconds>[\d.]+)s "
)
MODULE_LOADING_LINE = re.compile(r" Loading module \S+ \((?P<current>\d+)/(?P<total>\d+)\)$")
TEST_START_LINE = re.compile(r" Starting [\w.]+\.\w+ \.\.\.$")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

PROBLEM_LEVELS = ("WARNING", "ERROR", "CRITICAL")

# Log records closing the test class running before them: the next phase starts or the run is over.
PHASE_LOGGERS = (
    "odoo.addons.base.models.assetsbundle",
    "odoo.modules.loading",
    "odoo.service.server",
    "odoo.tests.result",
)


@dataclass
class LogRecord:
    """A log record of odoo-bin, with the lines printed after it (a traceback, the rest of a message)."""

    timestamp: datetime
    level: str
    database: str
    logger: str
    message: str
    details: list[str] = field(default_factory=list)

    @property
    def test_module(self) -> str | None:
        """The Odoo module whose tests logged this record, if any."""
        match = TEST_LOGGER.match(self.logger)
        return match.group("module") if match else None

    @property
    def text(self) -> str:
        return "\n".join([self.message, *self.details])

    @property
    def text_without_database(self) -> str:
        """The text of the record, the name of the database replaced so that runs on other databases compare equal."""
        return self.text if self.database == "?" else self.text.replace(self.database, "<database>")


@dataclass
class TestCounts:
    run: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0

    @property
    def passed(self) -> int:
        return self.run - self.failed - self.errors - self.skipped

    def __add__(self, other: "TestCounts") -> "TestCounts":
        return TestCounts(
            self.run + other.run,
            self.failed + other.failed,
            self.errors + other.errors,
            self.skipped + other.skipped,
        )


def read_records(path: Path) -> Iterator[LogRecord]:
    """Iterate over the log records of a log file; lines printed before the first record are dropped."""
    record: LogRecord | None = None

    with path.open(encoding="utf-8", errors="replace") as log_file:
        for raw_line in log_file:
            line = ANSI_ESCAPE.sub("", raw_line.rstrip("\n").replace("\r", ""))
            match = LOG_LINE.match(line)

            if match is None:
                if record is not None:
                    record.details.append(line)
                continue

            if record is not None:
                yield record

            record = LogRecord(
                timestamp=datetime.strptime(match.group("timestamp"), "%Y-%m-%d %H:%M:%S,%f"),
                level=match.group("level"),
                database=match.group("database"),
                logger=match.group("logger"),
                message=match.group("message"),
            )

    if record is not None:
        yield record


def started_test(record: LogRecord) -> tuple[str, str, str] | None:
    """The (module, class, method) of the test a record announces the start of, if it does."""
    module = record.test_module
    match = TEST_START.match(record.message) if module else None
    return (module, match.group("test_class"), match.group("method")) if match else None


def started_tests(records: Iterable[LogRecord]) -> list[tuple[str, str, str]]:
    return [test for test in map(started_test, records) if test is not None]


def count_tests(records: Iterable[LogRecord]) -> TestCounts | None:
    """The counts of the run, or None if the run ended before reporting them (a crash)."""
    counts: TestCounts | None = None
    skipped = 0

    for record in records:
        if record.test_module and TEST_SKIP.match(record.message):
            skipped += 1
        elif record.logger == "odoo.tests.result" and (match := RESULT.match(record.message)):
            counts = TestCounts(int(match.group("run")), int(match.group("failed")), int(match.group("errors")))

    if counts is not None:
        counts.skipped = skipped

    return counts


def is_failure(record: LogRecord) -> bool:
    return record.level == "ERROR" and bool(record.test_module) and bool(TEST_FAILURE.match(record.message))


def is_problem(record: LogRecord) -> bool:
    """Whether a record is a warning or error other than a test failure or the summary of the run."""
    return record.level in PROBLEM_LEVELS and record.logger != "odoo.tests.result" and not is_failure(record)


def test_durations(records: Iterable[LogRecord]) -> dict[tuple[str, str, str], float]:
    """Seconds each test took, keyed by (module, class, method).

    Read from the detailed report `odoo.tests.stats` logs at the end of the run when set to the DEBUG level.
    """
    durations: dict[tuple[str, str, str], float] = {}

    for record in records:
        if record.logger != "odoo.tests.stats" or not record.message.startswith("Detailed Tests Report"):
            continue

        for line in record.details:
            if match := TEST_STAT.match(line):
                durations[match.group("module"), match.group("test_class"), match.group("method")] = float(
                    match.group("seconds")
                )

    return durations


def class_durations(records: Iterable[LogRecord]) -> dict[tuple[str, str], float]:
    """Seconds spent in each test class, keyed by (module, class).

    A class is charged with the time from the end of the test run before its own until the end of its own, so its
    setUpClass, which runs before its first test is announced, is charged to it and not to the class before it.
    A test ends after its reported duration, or at the last line its loggers wrote if later: the duration Odoo
    reports for a JS suite leaves out the time its browser runs.
    """
    records = list(records)
    own_durations = test_durations(records)
    durations: dict[tuple[str, str], float] = defaultdict(float)
    previous_end: datetime | None = None
    current_class: tuple[str, str] | None = None
    current_logger = ""

    for record in records:
        test = started_test(record)

        if test is None:
            if record.logger in PHASE_LOGGERS:
                previous_end, current_class = record.timestamp, None
            elif (
                current_class is not None
                and previous_end is not None
                and record.logger.startswith(current_logger)
                and record.timestamp > previous_end
            ):
                durations[current_class] += (record.timestamp - previous_end).total_seconds()
                previous_end = record.timestamp
            continue

        end = record.timestamp + timedelta(seconds=own_durations.get(test, 0.0))

        if previous_end is not None:
            durations[test[:2]] += (end - previous_end).total_seconds()

        previous_end, current_class, current_logger = end, test[:2], record.logger

    return dict(durations)
