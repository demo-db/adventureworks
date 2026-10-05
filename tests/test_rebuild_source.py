import importlib.util
import json
import sqlite3
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

    def test_fixed_point_values_are_validated_and_kept_as_source_text(self):
        column = {
            "name": "Amount",
            "sourceType": "decimal",
            "nullable": True,
            "logicalType": "decimal",
            "precision": 38,
            "scale": 4,
            "sqliteStorage": "text",
            "computedExpression": None,
        }
        for value in (b"123456789012345678901234567890.1200", b"-0.0000", b"0.0000"):
            self.assertEqual(REBUILD.value_for(value, column), value.decode("ascii"))
        self.assertIsNone(REBUILD.value_for(b"", column))
        with self.assertRaisesRegex(REBUILD.ConversionError, "missing fixed-point"):
            REBUILD.value_for(b"", {**column, "nullable": False})
        with self.assertRaisesRegex(REBUILD.ConversionError, "exceeds"):
            REBUILD.value_for(b"1.00001", column)
        with self.assertRaisesRegex(REBUILD.ConversionError, "invalid fixed-point"):
            REBUILD.value_for(b"not-a-decimal", column)
        for malformed in (b"1e4", b" 1.00", b"1.0 "):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(REBUILD.ConversionError, "invalid fixed-point"):
                REBUILD.value_for(malformed, column)

    def test_fixed_point_declared_integer_capacity_and_sql_server_money_ranges(self):
        column = {
            "name": "Amount", "sourceType": "decimal", "nullable": False,
            "logicalType": "decimal", "precision": 8, "scale": 2,
        }
        self.assertEqual(REBUILD.value_for(b"123456.78", column), "123456.78")
        with self.assertRaisesRegex(REBUILD.ConversionError, "exceeds"):
            REBUILD.value_for(b"1234567.89", column)
        zero = {**column, "precision": 1, "scale": 1}
        self.assertEqual(REBUILD.value_for(b"0", zero), "0")
        money = {**column, "sourceType": "money", "precision": 19, "scale": 4}
        self.assertEqual(REBUILD.value_for(b"-922337203685477.5808", money), "-922337203685477.5808")
        with self.assertRaisesRegex(REBUILD.ConversionError, "money range"):
            REBUILD.value_for(b"922337203685477.5808", money)
        smallmoney = {**column, "sourceType": "smallmoney", "precision": 10, "scale": 4}
        self.assertEqual(REBUILD.value_for(b"214748.3647", smallmoney), "214748.3647")
        with self.assertRaisesRegex(REBUILD.ConversionError, "smallmoney range"):
            REBUILD.value_for(b"214748.3648", smallmoney)

    def test_decimal_check_evaluator_enforces_ranges_branches_and_sql_null(self):
        rate = '"Rate" BETWEEN 6.50 AND 200.00'
        self.assertTrue(REBUILD.validate_check_expression(rate, {"Rate": "6.50"}))
        self.assertTrue(REBUILD.validate_check_expression(rate, {"Rate": "200.00"}))
        self.assertFalse(REBUILD.validate_check_expression(rate, {"Rate": "6.49"}))
        self.assertIsNone(REBUILD.validate_check_expression(rate, {"Rate": None}))
        self.assertTrue(REBUILD.validate_check_expression(rate + "   ", {"Rate": "10.00"}))
        bill_of_materials = '(("ProductAssemblyID" IS NULL) AND ("BOMLevel" = 0) AND ("PerAssemblyQty" = 1.00)) OR (("ProductAssemblyID" IS NOT NULL) AND ("BOMLevel" >= 1))'
        self.assertTrue(REBUILD.validate_check_expression(bill_of_materials, {"ProductAssemblyID": None, "BOMLevel": 0, "PerAssemblyQty": "1.00"}))
        self.assertTrue(REBUILD.validate_check_expression(bill_of_materials, {"ProductAssemblyID": 7, "BOMLevel": 1, "PerAssemblyQty": "2.00"}))
        self.assertFalse(REBUILD.validate_check_expression(bill_of_materials, {"ProductAssemblyID": None, "BOMLevel": 0, "PerAssemblyQty": "2.00"}))
        with self.assertRaisesRegex(REBUILD.ConversionError, "unsupported SQL Server CHECK"):
            REBUILD.validate_check_expression('"Amount" LIKE \'%\'', {"Amount": "1.00"})
        with self.assertRaisesRegex(REBUILD.ConversionError, "unavailable column"):
            REBUILD.validate_check_expression('"Missing" >= 0', {"Amount": "1.00"})
        with self.assertRaisesRegex(REBUILD.ConversionError, "unsupported SQL Server CHECK"):
            REBUILD.validate_check_expression('"Amount" >>> 0', {"Amount": "1.00"})

    def test_native_fixed_point_declarations_keep_sql_server_precision_and_scale(self):
        self.assertEqual(
            REBUILD.fixed_point_declaration({
                "name": "Cost",
                "sourceType": "decimal",
                "sourceDefinition": "[Cost] [decimal](9, 4) NOT NULL",
            }),
            (9, 4),
        )
        self.assertEqual(
            REBUILD.fixed_point_declaration({"name": "Price", "sourceType": "money", "sourceDefinition": "[Price] [money] NOT NULL"}),
            (19, 4),
        )
        self.assertEqual(
            REBUILD.fixed_point_declaration({"name": "Rate", "sourceType": "smallmoney", "sourceDefinition": "[Rate] [smallmoney] NOT NULL"}),
            (10, 4),
        )

    def test_computed_decimal_snapshot_scale_is_explicitly_not_native_inference(self):
        column = {"name": "TotalDue", "computedExpression": "ISNULL([Subtotal] + [TaxAmt], 0.00)"}
        result = REBUILD.computed_decimal_snapshot(column, [[b"123.4500"], [b"0.0000"]], 0, {"Subtotal", "TaxAmt"})
        self.assertEqual(result, (38, 4))
        self.assertIsNone(REBUILD.computed_decimal_snapshot(column, [[b"12.50"]], 0, {"Quantity"}))
        with self.assertRaisesRegex(REBUILD.ConversionError, "storage precision 38"):
            REBUILD.computed_decimal_snapshot(column, [[b"123456789012345678901234567890123456.123"]], 0, {"Subtotal"})
        with self.assertRaisesRegex(REBUILD.ConversionError, "invalid computed decimal"):
            REBUILD.computed_decimal_snapshot(column, [[b"1e2"]], 0, {"Subtotal"})

    def test_decimal_sqlite_declaration_has_text_affinity(self):
        column = {
            "name": "Amount",
            "sourceType": "decimal",
            "nullable": False,
            "logicalType": "decimal",
            "precision": 10,
            "scale": 2,
            "sqliteStorage": "text",
            "computedExpression": None,
        }
        declaration = REBUILD.sqlite_column_sql(column)
        self.assertEqual(declaration, '"Amount" DECIMAL_TEXT(10,2) NOT NULL')
        with sqlite3.connect(":memory:") as db:
            db.execute(f"CREATE TABLE amounts ({declaration})")
            db.execute('INSERT INTO amounts ("Amount") VALUES (?)', ("12345678.90",))
            self.assertEqual(db.execute('SELECT typeof("Amount"), "Amount" FROM amounts').fetchone(), ("text", "12345678.90"))


if __name__ == "__main__":
    unittest.main()
