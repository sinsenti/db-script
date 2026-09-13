import unittest

from dbcli.adapters.base import InvalidIdentifierError, RowIdentityError
from dbcli.query import (
    build_filter_clause,
    build_order_clause,
    build_primary_key_where,
    quote_identifier,
)


class SqlHelperTests(unittest.TestCase):
    def test_quote_identifier_escapes_delimiters(self):
        self.assertEqual(quote_identifier('a"b'), '"a""b"')
        self.assertEqual(quote_identifier("a`b", "mysql"), "`a``b`")

    def test_quote_identifier_rejects_empty_and_nul(self):
        with self.assertRaises(InvalidIdentifierError):
            quote_identifier("")
        with self.assertRaises(InvalidIdentifierError):
            quote_identifier("bad\x00name")

    def test_order_clause_allow_lists_column_and_direction(self):
        self.assertEqual(
            build_order_clause(("name", "desc"), ["id", "name"]),
            'ORDER BY "name" DESC',
        )
        with self.assertRaises(InvalidIdentifierError):
            build_order_clause(("name; DROP TABLE users", "ASC"), ["name"])
        with self.assertRaises(ValueError):
            build_order_clause(("name", "SIDEWAYS"), ["name"])

    def test_plain_text_filter_uses_bound_parameters(self):
        clause, params = build_filter_clause("O'Reilly", ["name", "email"])
        self.assertIn('CAST("name" AS TEXT) LIKE ?', clause)
        self.assertEqual(params, ["%O'Reilly%", "%O'Reilly%"])
        self.assertNotIn("O'Reilly", clause)

    def test_non_sqlite_filter_uses_driver_placeholder(self):
        clause, params = build_filter_clause("Ada", ["name"], dialect="postgres")
        self.assertIn('CAST("name" AS TEXT) LIKE %s', clause)
        self.assertEqual(params, ["%Ada%"])

    def test_primary_key_where_supports_composite_keys(self):
        clause, params = build_primary_key_where(
            {"tenant_id": 7, "user_id": 42},
            ["tenant_id", "user_id"],
        )
        self.assertEqual(clause, '"tenant_id" = ? AND "user_id" = ?')
        self.assertEqual(params, [7, 42])

    def test_primary_key_where_requires_identity(self):
        with self.assertRaises(RowIdentityError):
            build_primary_key_where({"id": 1}, [])
        with self.assertRaises(RowIdentityError):
            build_primary_key_where({"name": "Ada"}, ["id"])


if __name__ == "__main__":
    unittest.main()
