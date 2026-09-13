import csv
import json
import tempfile
import unittest
from pathlib import Path

from dbcli.export import export_rows_to_csv, export_rows_to_json, export_rows_to_sql


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name)
        self.headers = ["id", "name", "metadata"]
        self.rows = [(1, "O'Reilly", '{"active": true}')]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_json_export_decodes_json_columns(self):
        path = export_rows_to_json(
            self.headers, self.rows, "users", output_dir=self.output_dir
        )
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            [{"id": 1, "name": "O'Reilly", "metadata": {"active": True}}],
        )

    def test_csv_export_uses_csv_quoting(self):
        path = export_rows_to_csv(
            self.headers, self.rows, "users", output_dir=self.output_dir
        )
        with path.open(newline="", encoding="utf-8") as output_file:
            self.assertEqual(
                list(csv.reader(output_file)),
                [self.headers, ["1", "O'Reilly", '{"active": true}']],
            )

    def test_sql_export_escapes_values_and_identifiers(self):
        path = export_rows_to_sql(
            self.headers, self.rows, 'user"data', "users", self.output_dir
        )
        content = path.read_text(encoding="utf-8")
        self.assertIn('INSERT INTO "user""data"', content)
        self.assertIn("'O''Reilly'", content)


if __name__ == "__main__":
    unittest.main()
