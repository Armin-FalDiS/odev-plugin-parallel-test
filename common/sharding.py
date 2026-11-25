"""Spread test classes over shards so that the shards finish at about the same time."""

import json
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_SECONDS_PER_TEST = 0.5
"""Estimate for classes never measured, used until a first run has measured any class at all."""

MAX_TEST_TAGS_LENGTH = 100_000
"""Linux refuses a single command-line argument longer than 128 KiB; the test tags of a shard go in one."""


@dataclass
class WorkUnit:
    """The tests of one class to run in the same shard, so that its setUpClass runs only once."""

    module: str
    test_class: str
    methods: list[str]
    class_size: int
    """Number of tests in the class when run as a whole."""

    tag_modules: Sequence[str]
    """Modules to name in the test tags: the module of the class, or for a cross-module test (the JS suites of
    `web`) the modules whose tests it runs."""

    estimate: float = 0.0
    """Estimated duration, in seconds."""

    @property
    def key(self) -> str:
        return f"{self.module}:{self.test_class}"

    @property
    def is_whole_class(self) -> bool:
        return len(self.methods) >= self.class_size

    @property
    def test_tags(self) -> list[str]:
        """Test tags selecting exactly these tests, whatever their tags (`*` lifts the implicit `standard`)."""
        classes = [f"*/{module}:{self.test_class}" for module in self.tag_modules]
        if self.is_whole_class:
            return classes
        return [f"{test_class}.{method}" for test_class in classes for method in self.methods]


@dataclass
class Shard:
    index: int
    units: list[WorkUnit] = field(default_factory=list)

    @property
    def estimate(self) -> float:
        return sum(unit.estimate for unit in self.units)

    @property
    def test_count(self) -> int:
        return sum(len(unit.methods) for unit in self.units)

    @property
    def test_tags(self) -> str:
        return ",".join(tag for unit in self.units for tag in unit.test_tags)


class TimingStore:
    """Durations measured for each test class in earlier runs, kept in a JSON file.

    Each class is stored as the seconds it took and the number of tests that ran, so that a run selecting only
    part of a class can be estimated from it.
    """

    def __init__(self, path: Path):
        self.path = path
        self.timings: dict[str, dict[str, float]] = self._load()

    def _load(self) -> dict[str, dict[str, float]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.timings, indent=2, sort_keys=True), encoding="utf-8")

    def record(self, key: str, seconds: float, test_count: int):
        if test_count:
            self.timings[key] = {"seconds": round(seconds, 2), "tests": test_count}

    def seconds_per_test(self, key: str) -> float | None:
        timing = self.timings.get(key)
        return timing["seconds"] / timing["tests"] if timing else None

    def default_seconds_per_test(self) -> float:
        known = [timing["seconds"] / timing["tests"] for timing in self.timings.values()]
        return statistics.median(known) if known else DEFAULT_SECONDS_PER_TEST

    def estimate(self, units: Sequence[WorkUnit]):
        fallback = self.default_seconds_per_test()

        for unit in units:
            seconds_per_test = self.seconds_per_test(unit.key) or fallback
            unit.estimate = seconds_per_test * len(unit.methods)


def default_shard_count(units: Sequence[WorkUnit], cpu_count: int) -> int:
    """Half the CPUs, but no more shards than can still shorten the run.

    Each shard keeps about one CPU busy with odoo-bin and half of one with PostgreSQL. A run can never be shorter
    than its longest class, so shards beyond total / longest only add start-up time.
    """
    if not units:
        return 1

    longest = max(unit.estimate for unit in units)
    useful = math.ceil(sum(unit.estimate for unit in units) / longest) if longest else len(units)
    return max(1, min(cpu_count // 2, useful, len(units)))


def split(units: Sequence[WorkUnit], shard_count: int) -> list[Shard]:
    """Longest class first, each one to the shard with the least work so far."""
    shards = [Shard(index) for index in range(1, shard_count + 1)]

    for unit in sorted(units, key=lambda unit: unit.estimate, reverse=True):
        min(shards, key=lambda shard: shard.estimate).units.append(unit)

    return [shard for shard in shards if shard.units]


def group_by_class(tests: Sequence[Sequence[str]], class_sizes: Mapping[str, int]) -> list[WorkUnit]:
    units: dict[str, WorkUnit] = {}

    for module, test_class, method, tag_modules in tests:
        key = f"{module}:{test_class}"
        unit = units.setdefault(key, WorkUnit(module, test_class, [], class_sizes.get(key, 0), tag_modules))
        unit.methods.append(method)

    return list(units.values())
