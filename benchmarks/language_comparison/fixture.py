#!/usr/bin/env python3
"""Create deterministic disposable SQLite fixtures for all comparison candidates."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
from pathlib import Path

SEED = 20250308
COUNTS = {"small": 128, "large": 10_000}


def create_fixture(path: Path, size: str) -> dict:
    people_count = COUNTS[size]
    rng = random.Random(SEED)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript("""
            CREATE TABLE people (
                id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                city TEXT,
                note TEXT,
                profile_json TEXT,
                payload BLOB,
                score INTEGER NOT NULL,
                active INTEGER,
                nullable TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX idx_people_city_score ON people(city, score);
            CREATE TABLE orders (
                order_id INTEGER NOT NULL,
                line_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL REFERENCES people(id),
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                attributes TEXT,
                PRIMARY KEY (order_id, line_id)
            );
            CREATE INDEX idx_orders_person ON orders(person_id);
            CREATE TABLE loose_tags (
                tag TEXT,
                person_id INTEGER REFERENCES people(id),
                value TEXT
            );
            CREATE TABLE edit_targets (
                id INTEGER PRIMARY KEY,
                label TEXT NOT NULL
            );
        """)
        people = []
        names = [("Ada Lovelace", "München", "O'Brien · 雪"),
                 ("Grace Hopper", "New York", "quoted 'value'"),
                 ("李小龍", "香港", "naïve café"),
                 ("Zoë Saldaña", "Bogotá", "line\nbreak")]
        for ident in range(1, people_count + 1):
            if ident <= len(names):
                name, city, note = names[ident - 1]
            else:
                name = f"Person {ident:05d}"
                city = ("New York", "München", "Bogotá", "東京", "Nairobi")[ident % 5]
                note = f"note {rng.randrange(1_000_000)}"
            profile = {"index": ident, "role": "engineer" if ident % 2 else "analyst",
                       "tags": ["seeded", "雪"] if ident == 1 else [f"g{ident % 7}"]}
            if ident == 1:
                payload = bytes((0, 255, 39, 10, 128))
            else:
                payload = bytes(rng.randrange(256) for _ in range(5 + ident % 8))
            people.append((ident, name, city, note,
                           json.dumps(profile, ensure_ascii=False, separators=(",", ":")),
                           payload, (ident * 37) % 1000, ident % 2,
                           None if ident % 9 == 0 else f"optional-{ident % 13}",
                           f"2025-03-{(ident % 28) + 1:02d}T12:00:00Z"))
        con.executemany("INSERT INTO people VALUES (?,?,?,?,?,?,?,?,?,?)", people)
        order_rows = []
        for ident in range(1, people_count + 1):
            for line_id in (1, 2):
                sku = "Café-雪" if ident == 1 and line_id == 1 else (
                    "O'Brien" if ident == 2 else f"SKU-{(ident * 3 + line_id) % 997:03d}")
                attributes = {"gift": bool(ident % 2), "line": line_id,
                              "label": "naïve" if ident == 1 else "standard"}
                order_rows.append((ident, line_id, ident, sku,
                                   (ident + line_id) % 11 + 1,
                                   json.dumps(attributes, ensure_ascii=False, separators=(",", ":"))))
        con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?)", order_rows)
        con.executemany("INSERT INTO edit_targets VALUES (?,?)", [
            (1, "original key"), (2, "stable row"), (3, "delete target")
        ])
        con.executemany("INSERT INTO loose_tags VALUES (?,?,?)", [
            ("priority", 1, "O'Brien"), ("雪", 2, None), (None, None, "no identity")
        ])
        con.commit()
    finally:
        con.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": str(path), "size": size, "seed": SEED,
            "people_rows": people_count, "orders_rows": people_count * 2,
            "sqlite_version": sqlite3.sqlite_version, "sha256": digest,
            "bytes": path.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", choices=COUNTS, default="small")
    parser.add_argument("--output", type=Path, default=Path("build/fixtures"))
    args = parser.parse_args()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    manifests = [create_fixture(output / f"fixture_{size}.sqlite", size)
                 for size in ([args.size] if args.size else COUNTS)]
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifests, indent=2) + "\n", encoding="utf-8")
    print(manifest_path.read_text(encoding="utf-8"), end="")


if __name__ == "__main__":
    main()
