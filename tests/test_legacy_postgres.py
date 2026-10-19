import json
import unittest
from unittest.mock import patch

from dbcli import legacy


class LegacyPostgresSchemaTests(unittest.TestCase):
    def test_docker_table_list_includes_user_schemas(self):
        output = "\n".join(
            [
                json.dumps(["app", "leads"]),
                json.dumps(["public", "users"]),
            ]
        )
        with (
            patch.object(legacy, "get_container_env_user", return_value="postgres"),
            patch.object(legacy, "run_docker_cmd", return_value=output) as run,
        ):
            tables = legacy.get_postgres_tables("kvarta-db-1", "kvarta")

        self.assertEqual(tables, ["app.leads", "public.users"])
        sql = run.call_args.args[0][-1]
        self.assertIn("FROM pg_tables", sql)
        self.assertIn("schemaname !~ '^pg_'", sql)
        self.assertNotIn("schemaname='public'", sql)

    def test_direct_postgres_table_list_includes_user_schemas(self):
        output = json.dumps(["app", "leads"])
        with patch.object(
            legacy, "run_docker_cmd_with_err", return_value=(output, None)
        ) as run:
            tables = legacy.get_direct_postgres_tables("postgresql://localhost/kvarta")

        self.assertEqual(tables, ["app.leads"])
        self.assertIn("FROM pg_tables", run.call_args.args[0][-1])

    def test_schema_qualified_table_is_quoted_as_two_identifiers(self):
        self.assertEqual(
            legacy._postgres_table_reference('"odd.schema"."lead""s"'),
            '"odd.schema"."lead""s"',
        )
        self.assertEqual(
            legacy._postgres_table_reference("app.leads"), '"app"."leads"'
        )

    def test_docker_page_query_uses_schema_qualified_table(self):
        with (
            patch.object(legacy, "get_container_env_user", return_value="postgres"),
            patch.object(
                legacy, "run_docker_cmd_with_err", return_value=("id\n1\n", None)
            ) as run,
        ):
            headers, rows = legacy.query_postgres_page(
                "kvarta-db-1", "kvarta", "app.leads", 25, 0
            )

        sql = run.call_args.args[0][-1]
        self.assertIn('FROM "app"."leads"', sql)
        self.assertEqual(headers, ["id"])
        self.assertEqual(rows, [["1"]])

    def test_schema_metadata_is_scoped_to_selected_schema(self):
        with (
            patch.object(legacy, "get_container_env_user", return_value="postgres"),
            patch.object(legacy, "run_docker_cmd", return_value="id|integer|NO|") as run,
        ):
            headers, rows = legacy.get_postgres_schema_info(
                "kvarta-db-1", "kvarta", "app.leads"
            )

        sql = run.call_args.args[0][-1]
        self.assertIn("table_schema = 'app'", sql)
        self.assertIn("table_name = 'leads'", sql)
        self.assertEqual(headers[0], "Column Name")
        self.assertEqual(rows[0][0], "id")

    def test_foreign_key_targets_preserve_their_schema(self):
        output = "owner_id|crm|owners|id"
        with (
            patch.object(legacy, "get_container_env_user", return_value="postgres"),
            patch.object(legacy, "run_docker_cmd", return_value=output),
        ):
            fks = legacy.get_foreign_keys(
                "postgres", "kvarta-db-1", "kvarta", "app.leads"
            )

        self.assertEqual(
            fks,
            [{"from_col": "owner_id", "to_table": "crm.owners", "to_col": "id"}],
        )


if __name__ == "__main__":
    unittest.main()
