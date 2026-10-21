# db-script

A keyboard-driven terminal browser for SQLite, PostgreSQL and MySQL.

## Start

```sh
python ~/git/personal/db-script/db.py
```

This opens **Interactive Database Browser & Explorer**. Pick a local SQLite file
or running Docker database container. SQLite files in your current directory are
detected automatically; **Open a SQLite file…** lets you choose a file elsewhere
with path completion. **Refresh database list** rescans files and containers.

SQLite and PostgreSQL open in the native Rust/Ratatui browser. PostgreSQL
containers first offer a database picker, then Rust's table sidebar. Credentials
are resolved in memory; Docker PostgreSQL needs a local published port and usable
container credentials. An existing `DBCLI_POSTGRES_URL` also appears in the picker.
No Python PostgreSQL driver or host `psql` is needed for the Rust flow.

Closing the Rust browser returns to the picker; `q` in the picker exits. Sessions
are writable by default. `E` retains the Neovim diff and confirmation workflow.
MySQL entries use the original Python browser and are labeled accordingly.

Build the Rust executables once from this source checkout:

```sh
bash tools/build_rust.sh
```

The launcher resolves executable paths relative to the script, so it works from
any current directory. [Rust build, controls and benchmark guide](RUST.md).

## Other commands

```sh
python db.py --read-only                     # Picker and read-only Rust session
python db.py path/to/database.sqlite         # Open SQLite directly in Rust
python db.py --python                        # Original Python picker/browser
python db.py --python path/to/database.sqlite
```

The installed `dbcli` console command uses the same entry point. The Rust default
expects binaries built in this source checkout; `--python` also works in a
Python-only installation. Explicit PostgreSQL/MySQL URL targets remain supported;
prefer `DBCLI_POSTGRES_URL` to keep PostgreSQL credentials out of shell arguments.

For the original Python browser, install optional native drivers with
`pip install 'db-script[postgres,mysql]'`. Direct URLs prefer native drivers when
available. `--cli` selects the original Python interface with the `psql`/`mysql`
fallback. Legacy Docker paths retain their `docker exec` implementation and do not
require published host ports.

## Checks

```sh
DBCLI_RUN_INTEGRATION=0 python -m unittest discover -s tests -q
python -m unittest discover -s tools -p 'test_*.py' -q
```

Optional native-adapter integration tests use disposable Docker fixtures in
`tests/docker-compose.yml`; see `tests/test_integration_adapters.py`.
Real picker-to-Rust terminal checks are opt-in; see [RUST.md](RUST.md).

## Agent context

See [AGENTS.md](AGENTS.md) for project architecture, development commands,
safety constraints and known implementation gaps.
