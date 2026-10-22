import unittest
from unittest.mock import Mock, patch

from dbcli import ui


class NavigationTests(unittest.TestCase):
    def test_h_does_not_close_picker(self):
        with patch.object(ui, "draw_frame"), patch.object(ui, "get_key", side_effect=["h", "j", "ENTER"]) as keys:
            self.assertEqual(ui.select_menu_item("Choose", ["one", "two"]), ("two", 1))
            self.assertEqual(keys.call_count, 3)

    def test_h_does_not_close_column_picker(self):
        hidden = set()
        with patch.object(ui, "draw_frame"), patch.object(ui, "get_key", side_effect=["h", "SPACE", "q"]) as keys:
            ui.toggle_column_visibility(["id", "name"], hidden)
            self.assertEqual(keys.call_count, 3)
        self.assertEqual(hidden, {"id"})

    def test_h_does_not_close_row_detail(self):
        with patch.object(ui, "draw_frame"), patch.object(ui, "get_key", side_effect=["h", "q"]) as keys:
            self.assertFalse(ui.browse_row_detail(["id"], (1,), 1))
            self.assertEqual(keys.call_count, 2)

    def test_h_does_not_close_table_browser(self):
        query = Mock(return_value=(["id"], [(1,)]))
        with patch.object(ui, "draw_frame"), patch.object(ui, "get_key", side_effect=["h", "q"]) as keys:
            ui.browse_table(query, table_name="fixture", total_rows=1, read_only=True)
            self.assertEqual(keys.call_count, 2)
        self.assertEqual(query.call_count, 2)


if __name__ == "__main__":
    unittest.main()
