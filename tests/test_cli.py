import unittest

from db import parse_cli_args


class CliArgumentTests(unittest.TestCase):
    def test_target_and_read_only_flag(self):
        args = parse_cli_args(["sample.db", "--read-only"])
        self.assertEqual(args.target, "sample.db")
        self.assertTrue(args.read_only)

    def test_target_is_optional(self):
        args = parse_cli_args([])
        self.assertIsNone(args.target)
        self.assertFalse(args.read_only)


if __name__ == "__main__":
    unittest.main()
