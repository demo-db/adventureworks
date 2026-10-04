import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("rebuild_source", ROOT / "scripts" / "rebuild-source.py")
REBUILD = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(REBUILD)


class SourceConversionTests(unittest.TestCase):
    def test_checked_native_view_inventory_is_complete_and_separates_sqlite_views(self):
        native = json.loads((ROOT / "metadata" / "native-objects.json").read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "metadata" / "schema.json").read_text(encoding="utf-8"))
        native_views = native["views"]
        source_views = schema["sourceViews"]
        self.assertEqual(len(native_views), 20)
        self.assertEqual(len(source_views), 20)
        self.assertEqual(sum(bool(view["availableAsSqliteView"]) for view in source_views), 11)
        self.assertEqual(
            {view["recordset"] for view in native_views},
            {view["recordset"] for view in source_views},
        )
        self.assertEqual(
            {view["name"] for view in schema["tables"] if view["kind"] == "view"},
            {view["recordset"] for view in source_views if view["availableAsSqliteView"]},
        )

    def test_bracketed_like_class_is_not_rewritten_as_identifier(self):
        self.assertEqual(
            REBUILD.normalize_expression("[Shelf] LIKE '[A-Za-z]'"),
            '"Shelf" GLOB \'[A-Za-z]\'',
        )

    def test_upper_check_matches_sql_server_fixed_width_text(self):
        self.assertEqual(
            REBUILD.normalize_expression("UPPER([Class]) IN ('L', 'M', 'H')"),
            'UPPER(RTRIM("Class")) IN (\'L\', \'M\', \'H\')',
        )

    def test_dynamic_source_defaults_are_frozen_or_deterministic(self):
        self.assertEqual(
            REBUILD.literal_default("CONSTRAINT [DF_Date] DEFAULT (GETDATE())"),
            "'2025-11-14 12:13:16.797'",
        )
        self.assertEqual(
            REBUILD.literal_default("CONSTRAINT [DF_Guid] DEFAULT (NEWID())"),
            "NEWID()",
        )

    def test_sqlite_schema_freezes_getdate_and_omits_nonconstant_newid_default(self):
        base = {
            "name": "rowguid",
            "sourceType": "uniqueidentifier",
            "nullable": False,
            "computedExpression": None,
        }
        newid = REBUILD.sqlite_column_sql({**base, "defaultExpression": "NEWID()"})
        getdate = REBUILD.sqlite_column_sql({**base, "defaultExpression": "'2025-11-14 12:13:16.797'"})
        self.assertEqual(newid, '"rowguid" TEXT NOT NULL')
        self.assertEqual(getdate, '"rowguid" TEXT NOT NULL DEFAULT \'2025-11-14 12:13:16.797\'')

    def test_relational_view_translation_preserves_native_names(self):
        view = {
            "recordset": "Purchasing.vVendorWithContacts",
            "sqliteCompatibility": "unreviewed",
            "limitation": "",
            "sourceDefinition": (
                "CREATE VIEW [Purchasing].[vVendorWithContacts] AS SELECT v.[Name], "
                "ct.[Name] AS [ContactType] FROM [Purchasing].[Vendor] v "
                "JOIN [Person].ContactType ct ON ct.[ContactTypeID] = v.[BusinessEntityID]"
            ),
        }
        converted = REBUILD.sqlite_view_sql(view)
        self.assertIn('CREATE VIEW "Purchasing.vVendorWithContacts" AS', converted)
        self.assertIn('"Person.ContactType" ct', converted)
        self.assertIn('v."Name"', converted)

    def test_sql_server_only_view_is_not_advertised_as_sqlite(self):
        view = {
            "recordset": "Sales.vSalesPersonSalesByFiscalYears",
            "sqliteCompatibility": "unsupported",
            "limitation": "SQL Server PIVOT syntax",
            "sourceDefinition": "CREATE VIEW [Sales].[vSalesPersonSalesByFiscalYears] AS SELECT 1",
        }
        with self.assertRaises(REBUILD.ConversionError):
            REBUILD.sqlite_view_sql(view)

    def test_inline_document_rowguid_uniqueness_is_preserved(self):
        installer = (
            ROOT
            / "data-source"
            / "microsoft-sql-server-samples"
            / "adventure-works"
            / "oltp-install-script"
            / "instawdb.sql"
        ).read_text(encoding="utf-8-sig")
        tables = REBUILD.parse_tables(REBUILD.strip_comments(installer))
        indexes = REBUILD.parse_indexes(REBUILD.strip_comments(installer), tables)
        self.assertIn(
            {
                "name": "inline_unique_rowguid",
                "table": "Production.Document",
                "unique": True,
                "columns": ["rowguid"],
                "sourceKind": "inline column UNIQUE",
            },
            indexes,
        )


if __name__ == "__main__":
    unittest.main()
