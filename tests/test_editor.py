import json
import unittest
from pathlib import Path
from unittest.mock import patch

from dbcli.editor import launch_nvim_editor, launch_nvim_insert_editor


class EditorTests(unittest.TestCase):
    def test_row_editor_round_trips_json_fields(self):
        def fake_editor(path):
            file_path = Path(path)
            data = json.loads(file_path.read_text(encoding="utf-8"))
            data["metadata"]["active"] = True
            file_path.write_text(json.dumps(data), encoding="utf-8")
            return 0

        with patch("dbcli.editor._run_editor", side_effect=fake_editor):
            result = launch_nvim_editor(
                {"id": 1, "metadata": '{"active": false}', "name": "Ada"}
            )

        self.assertEqual(result["id"], 1)
        self.assertEqual(json.loads(result["metadata"]), {"active": True})
        self.assertEqual(result["name"], "Ada")

    def test_insert_editor_accepts_one_object(self):
        def fake_editor(path):
            Path(path).write_text(
                json.dumps({"id": None, "name": "New"}), encoding="utf-8"
            )
            return 0

        with patch("dbcli.editor._run_editor", side_effect=fake_editor):
            result = launch_nvim_insert_editor(["id", "name"])

        self.assertEqual(result, [{"id": None, "name": "New"}])


if __name__ == "__main__":
    unittest.main()
