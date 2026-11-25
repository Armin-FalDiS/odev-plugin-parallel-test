# ruff: noqa: F821 - run inside `odoo-bin shell`, where `env` is defined and `TEST_TAGS` is prepended by the command
"""List the tests selected by TEST_TAGS on the database, and pre-generate the assets bundles HttpCase tests need.

Odoo pre-generates the assets bundles at the start of every post_install run that contains an HttpCase. Doing it
once here, on the database every shard is cloned from, saves each shard that time.
"""

import json
import sys

from odoo.tests import loader
from odoo.tests.common import HttpCase
from odoo.tools import config


def class_size(test_class):
    """Number of test methods Odoo would run for a class when selected as a whole."""
    module = sys.modules[test_class.__module__]
    return sum(1 for test in loader.get_module_test_cases(module) if type(test) is test_class)


config["test_tags"] = TEST_TAGS
installed_modules = env["ir.module.module"].search([("state", "=", "installed")]).mapped("name")

selected = {}
needs_assets = False

for position in ("at_install", "post_install"):
    for test in loader.make_suite(installed_modules, position):
        # A cross-module test (the JS suites of `web`) runs the tests of the modules the tags name, not its own.
        tag_modules = tuple(test._test_modules) if hasattr(test, "_cross_module") else (test.test_module,)
        selected[(test.test_module, type(test).__name__, test._testMethodName, tag_modules)] = type(test)
        needs_assets = needs_assets or isinstance(test, HttpCase)

class_sizes = {}

for (module, class_name, _method, _tag_modules), test_class in selected.items():
    key = f"{module}:{class_name}"
    if key not in class_sizes:
        class_sizes[key] = class_size(test_class)

if needs_assets:
    env["ir.qweb"]._pregenerate_assets_bundles()
    env.cr.commit()

print(
    "PARALLEL-TEST-DISCOVERY "
    + json.dumps({"tests": [list(key) for key in selected], "class_sizes": class_sizes})
)
