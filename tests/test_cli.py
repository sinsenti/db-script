import unittest

from db import parse_cli_args


class CliArgumentTests(unittest.TestCase):
    def test_target_and_read_only_flag(self):
        args = parse_cli_args(["sample.db", "--read-only"])
        self.assertEqual(args.target, "sample.db")
        self.assertTrue(args.read_only)
        self.assertFalse(args.cli)

    def test_target_is_optional(self):
        args = parse_cli_args([])
        self.assertIsNone(args.target)
        self.assertFalse(args.read_only)
        self.assertFalse(args.cli)

    def test_cli_fallback_flag(self):
        args = parse_cli_args(["postgresql://user@host/db", "--cli"])
        self.assertTrue(args.cli)


if __name__ == "__main__":
    unittest.main()
