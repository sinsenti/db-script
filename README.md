# db-script

A keyboard-driven terminal browser for SQLite, PostgreSQL and MySQL.

## Usage

```sh
python db.py path/to/database.sqlite
python db.py --read-only path/to/database.sqlite
python db.py postgresql://user:password@host:5432/database
```

Install optional native drivers with `pip install 'db-script[postgres,mysql]'`.
Direct PostgreSQL and MySQL URLs use the native drivers when available; use
`--cli` to explicitly select the `psql`/`mysql` fallback.

Run tests with:

```sh
python -m unittest discover -s tests -q
```

Optional native-adapter integration tests use the disposable Docker fixtures
in `tests/docker-compose.yml`; see `tests/test_integration_adapters.py`.
