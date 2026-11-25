# odev-plugin-parallel-test

Run the tests of an Odoo database in parallel.

A test run executes every selected test class one after the other in a single `odoo-bin` process, which keeps one
CPU busy and leaves the rest of the machine idle. This plugin adds the `ptest` command, which spreads the test
classes over several copies of the database and runs them at the same time.

On a 12-CPU machine, the 800 tests of a large customer project went from about 11 minutes to a bit over 3.

## Installation

```sh
odev plugin --enable Armin-FalDiS/odev-plugin-parallel-test
```

## Usage

```sh
odev ptest DATABASE [ADDONS] [-u MODULES] [-i MODULES] [-t TAGS] [-j JOBS] [-k]
```

```sh
# Update partena_custom on a copy of a template and run its tests
odev ptest partena_custom:template ~/repos/my-project -u partena_custom

# Only some tests, on 4 databases
odev ptest my-database -u my_module -t /my_module:TestInvoicing,/my_module:TestPayment -j 4

# From scratch, on a new database
odev ptest my-new-database -V 18.0 -i my_module
```

Aliases: `odev parallel-test`.

| Option         | What it does                                                                                 |
| -------------- | -------------------------------------------------------------------------------------------- |
| `-u`, `--update` | Modules to update on the copy of the database before testing                               |
| `-i`, `--init`   | Modules to install on the copy of the database before testing                              |
| `-t`, `--tags`   | Test tags, as for `odoo-bin --test-tags`; default: all the tests of the `-u` and `-i` modules |
| `-j`, `--jobs`   | Number of databases testing at the same time; default: see below                            |
| `-k`, `--keep`   | Keep the databases created for the run, to investigate a failure                            |

`DATABASE` is never modified: when it exists, it is copied (a template, typically). When it does not exist, `-V` is
required and a new database is created.

## How it works

1. **Base database.** `DATABASE` is copied, and the `-u`/`-i` modules are updated or installed on the copy. Their
   `at_install` tests run there, during the update, as they would in a normal run.
2. **Discovery.** Odoo itself lists the tests the tags select (`odoo.tests.loader`), so the selection is exactly the
   one `odoo-bin --test-tags` would make. The assets bundles `HttpCase` tests need are generated once, here, instead
   of once per database.
3. **Split.** The tests are grouped by class, so that each `setUpClass` runs once, and the classes are spread over the
   databases longest first, each to the database with the least work so far. Durations come from earlier runs,
   stored in `parallel-test/timings.json` under odev's data directory (`~/odev` by default); the first run estimates them
   from the number of tests.
4. **Run.** The base database is cloned once per shard (a PostgreSQL template copy, about a second) and each clone
   runs its share with `--test-enable --test-tags`, without `-u`, on its own HTTP port.
5. **Report.** Failed tests with their tracebacks, the other warnings and errors (each one once, with the runs it
   appeared in), and a table of the tests run, passed, failed and skipped per run. The databases are deleted
   afterwards, the logs are kept in `/tmp/odev-parallel-test/<run>/`.

The command exits with an error when a test fails or a run stops before the end of its tests.

### Number of databases

Half the CPUs by default: each run keeps about one CPU busy with `odoo-bin` and half of one with PostgreSQL. The
number is lowered when more databases could not finish sooner: a run can never be shorter than its longest test
class.

### PostgreSQL

Every database of a PostgreSQL server shares one lock table, and a test transaction on a large database holds
thousands of locks. With the default settings (`max_locks_per_transaction = 64`), more than a few databases at once
run it out of slots, and tests fail at random with `out of shared memory`.

Before cloning, the command checks that the server can take the databases: lock slots, against the most locks a
run held in earlier runs (estimated from the number of tables and indexes until measured), and free connections.
When it cannot:

- with the default number of databases, the command runs fewer and prints the commands that would allow more;
- with `-j`, it prints the same commands and asks before running anyway (`-f` runs without asking).

```sh
sudo -u postgres psql -c "ALTER SYSTEM SET max_locks_per_transaction = '1024'"
sudo systemctl restart postgresql
```

It also suggests raising `shared_buffers` while it is under 1 GB (128 MB by default).

### Assets bundles

Before their first `HttpCase` test, Odoo generates the assets bundles, which takes about 30 seconds. The command
does it once on the base database, so the clones find them ready, and it takes a few seconds when the source
database already has them: generate them once in a template (`env['ir.qweb']._pregenerate_assets_bundles()` in
`odev shell`) and every run from it saves that time.

## Limitations

- A run on a copy of a database tests the code checked out now against data installed when the database was
  created: rebuild templates when they fall behind.
- Tests that depend on the order classes run in, or on what another class left in the database, can fail here and
  pass in a single run. That is a bug of the test, made visible.
- Test tags with parameters (`[...]`) are not passed on to the shards.
