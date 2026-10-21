"""Paths and credential-free argv for the selected native Rust implementation."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'rust/target/release/dbcli-rust'
FRONTEND = ROOT / 'ui/rust/target/release/dbcli-ui-rust'


def command(*, database=None, postgres=False, table=None, read_only=False, legacy=False):
    if postgres and database:
        raise ValueError('Choose --postgres or --database PATH')
    if not postgres:
        if not database:
            raise ValueError('Choose --postgres or --database PATH')
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise ValueError('SQLite database must be an existing file')
    if legacy:
        argv = [str(WORKER)]
        argv += ['--postgres'] if postgres else ['--db', str(path)]
    else:
        argv = [str(FRONTEND), '--backend', 'original-rust', '--worker', str(WORKER)]
        argv += ['--engine', 'postgres' if postgres else 'sqlite']
        if not postgres:
            argv += ['--database', str(path)]
    if table:
        argv += ['--table', table]
    if read_only:
        argv.append('--read-only')
    return argv
