#!/usr/bin/env python3
"""Build a deterministic SQLite fixture from Microsoft's pinned AW OLTP files.

This converter deliberately handles only the pinned AdventureWorks 2025
installer and its companion files. It is not a general T-SQL importer. The
installer's original DDL is retained as source metadata; SQL Server-only
objects such as XML methods, triggers, and procedures are not executed here.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "data-source" / "microsoft-sql-server-samples"
INSTALL_DIR = SOURCE_ROOT / "adventure-works" / "oltp-install-script"
INSTALLER = INSTALL_DIR / "instawdb.sql"
SOURCE_LOCK = ROOT / "data-source" / "source-lock.json"
OUTPUT = ROOT / "data-source" / "source.sqlite.gz"
NATIVE_METADATA = ROOT / "metadata" / "native-objects.json"
EXPECTED_UPSTREAM_REVISION = "beaab06ef72831089ca80e5355d65e661fd19b26"
SOURCE_PREFIX = "samples/databases/adventure-works/oltp-install-script/"
SOURCE_LOCK_PREFIX = "data-source/microsoft-sql-server-samples/"


class ConversionError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def strip_comments(text: str) -> str:
    """Remove T-SQL comments without changing string literals."""
    result: list[str] = []
    index = 0
    in_string = False
    while index < len(text):
        if in_string:
            result.append(text[index])
            if text[index] == "'":
                if index + 1 < len(text) and text[index + 1] == "'":
                    result.append(text[index + 1])
                    index += 2
                    continue
                in_string = False
            index += 1
            continue
        if text[index] == "'":
            result.append(text[index])
            in_string = True
            index += 1
        elif text.startswith("--", index):
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ConversionError("unterminated block comment in pinned installer")
            index = end + 2
        else:
            result.append(text[index])
            index += 1
    return "".join(result)


def split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    start = depth = 0
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            if in_string and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            in_string = not in_string
        elif not in_string:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append(text[start:index].strip())
                start = index + 1
        index += 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def matching_paren(text: str, opening: int) -> int:
    depth = 0
    in_string = False
    index = opening
    while index < len(text):
        char = text[index]
        if char == "'":
            if in_string and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            in_string = not in_string
        elif not in_string:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index
        index += 1
    raise ConversionError("unclosed parenthesized expression in pinned installer")


def quote_identifiers(expression: str) -> str:
    """Convert T-SQL bracket identifiers without rewriting string literals."""
    result: list[str] = []
    index = 0
    in_string = False
    while index < len(expression):
        if expression[index] == "'":
            result.append(expression[index])
            if in_string and index + 1 < len(expression) and expression[index + 1] == "'":
                result.append(expression[index + 1])
                index += 2
                continue
            in_string = not in_string
            index += 1
            continue
        if not in_string and expression[index] == "[":
            end = expression.find("]", index + 1)
            if end < 0:
                raise ConversionError("unterminated bracket identifier in expression")
            result.append(quote(expression[index + 1 : end]))
            index = end + 1
            continue
        result.append(expression[index])
        index += 1
    return "".join(result)


def type_name(declaration: str) -> str:
    match = re.match(r"(\[[^]]+\]|[A-Za-z][A-Za-z0-9_]*)(?:\s*\([^)]*\))?", declaration)
    if not match:
        raise ConversionError(f"cannot identify source type from {declaration!r}")
    return match.group(1).strip("[]").lower()


def sqlite_type(source_type: str, column_name: str) -> str:
    source_type = source_type.lower()
    if source_type in {"int", "integer", "smallint", "tinyint", "bit", "flag", "namestyle"}:
        return "INTEGER"
    if source_type in {"decimal", "numeric", "money", "smallmoney"}:
        return "NUMERIC"
    if source_type in {"float", "real"}:
        return "REAL"
    if source_type in {"binary", "varbinary", "image", "timestamp", "rowversion", "geography", "geometry", "hierarchyid"}:
        return "BLOB"
    if source_type == "xml":
        return "TEXT"
    if source_type in {
        "date", "datetime", "datetime2", "smalldatetime", "time", "char", "varchar",
        "nchar", "nvarchar", "text", "ntext", "uniqueidentifier", "sysname", "name",
        "flag", "accountnumber", "ordernumber", "namestyle", "phone",
    }:
        return "TEXT"
    if declaration_is_computed(source_type):
        if column_name.lower().endswith("level"):
            return "INTEGER"
        if column_name.lower() in {"accountnumber", "salesordernumber"}:
            return "TEXT"
        return "NUMERIC"
    raise ConversionError(f"unmapped source type {source_type!r} for {column_name!r}")


def declaration_is_computed(source_type: str) -> bool:
    return source_type.startswith("computed:")


def normalize_expression(expression: str) -> str:
    expression = re.sub(r"\bN'", "'", expression, flags=re.I)
    # SQL Server ignores trailing spaces in string equality comparisons. The
    # source's fixed-width ProductLine/Class/Style values include their CHAR
    # padding in the CSV, while SQLite compares those bytes literally.
    expression = re.sub(
        r"\bUPPER\s*\(\s*\[([^]]+)\]\s*\)",
        lambda match: f"UPPER(RTRIM({quote(match.group(1))}))",
        expression,
        flags=re.I,
    )
    expression = quote_identifiers(expression)
    expression = re.sub(r"\bISNULL\s*\(", "COALESCE(", expression, flags=re.I)
    # This source's only LIKE check is a one-character bracket class. SQLite
    # GLOB uses the same class syntax for this exact pattern.
    expression = re.sub(
        r"(\"[^\"]+\")\s+LIKE\s+'(\[[A-Za-z-]+\])'",
        r"\1 GLOB '\2'",
        expression,
        flags=re.I,
    )
    expression = re.sub(
        r"\bDATEADD\s*\(\s*YEAR\s*,\s*-\s*18\s*,\s*GETDATE\s*\(\s*\)\s*\)",
        "'2007-11-14 12:13:16.797'", expression, flags=re.I,
    )
    expression = re.sub(
        r"\bDATEADD\s*\(\s*DAY\s*,\s*1\s*,\s*GETDATE\s*\(\s*\)\s*\)",
        "'2025-11-15 12:13:16.797'", expression, flags=re.I,
    )
    expression = re.sub(r"\bGETDATE\s*\(\s*\)", "'2025-11-14 12:13:16.797'", expression, flags=re.I)
    expression = re.sub(r"\bNEWID\s*\(\s*\)", "lower(hex(randomblob(16)))", expression, flags=re.I)
    return expression.strip()


def literal_default(fragment: str) -> str | None:
    function_default = re.search(
        r"\bDEFAULT\s*\(\s*(GETDATE|NEWID)\s*\(\s*\)\s*\)",
        fragment,
        re.I,
    )
    if function_default:
        if function_default.group(1).upper() == "GETDATE":
            return "'2025-11-14 12:13:16.797'"
        return "NEWID()"
    match = re.search(r"\bDEFAULT\s*(\([^()]*\)|'(?:(?:'')|[^'])*'|[-+]?\d+(?:\.\d+)?|NULL)\s*$", fragment, re.I)
    if not match:
        return None
    value = match.group(1).strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    if re.fullmatch(r"(?i)NULL", value):
        return "NULL"
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
        return value
    if value.startswith("'") and value.endswith("'"):
        return value
    if re.fullmatch(r"(?i)GETDATE\s*\(\s*\)", value):
        return "'2025-11-14 12:13:16.797'"
    if re.fullmatch(r"(?i)NEWID\s*\(\s*\)", value):
        return "NEWID()"
    return None


def source_files() -> dict[str, bytes]:
    if not SOURCE_LOCK.is_file():
        raise ConversionError("data-source/source-lock.json is missing")
    lock = json.loads(SOURCE_LOCK.read_text(encoding="utf-8"))
    if lock.get("repository") != "https://github.com/microsoft/sql-server-samples":
        raise ConversionError("source lock repository changed")
    if lock.get("revision") != EXPECTED_UPSTREAM_REVISION:
        raise ConversionError("source lock revision changed")
    files = lock.get("files")
    expected_support_files = {
        "data-source/microsoft-sql-server-samples/LICENSE.txt",
        "data-source/microsoft-sql-server-samples/adventure-works/README.md",
        "data-source/microsoft-sql-server-samples/adventure-works/oltp-install-script/instawdb.sql",
    }
    if (
        not isinstance(files, dict)
        or len(files) != 72
        or not expected_support_files.issubset(files)
        or sum(path.endswith(".csv") for path in files) != 69
        or any(not path.startswith(SOURCE_LOCK_PREFIX) for path in files)
    ):
        raise ConversionError("source lock does not cover the complete pinned source tree")
    result: dict[str, bytes] = {}
    for relative, expected in sorted(files.items()):
        if Path(relative).is_absolute() or "\\" in relative or any(part in ("", ".", "..") for part in Path(relative).parts):
            raise ConversionError(f"pinned source path is unsafe: {relative!r}")
        path = ROOT / relative
        if not path.is_file():
            raise ConversionError(f"pinned source file is missing: {relative}")
        data = path.read_bytes()
        if sha256(data) != expected:
            raise ConversionError(f"pinned source file digest changed: {relative}")
        result[relative] = data
    return result


def parse_tables(source: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    position = 0
    while match := re.search(r"CREATE\s+TABLE\s+\[([^]]+)\]\.\[([^]]+)\]\s*\(", source[position:], re.I):
        schema, name = match.group(1), match.group(2)
        opening = position + match.end() - 1
        closing = matching_paren(source, opening)
        raw = source[opening + 1 : closing]
        columns: list[dict[str, Any]] = []
        checks: list[str] = []
        for definition in split_top_level(raw):
            col_match = re.match(r"\[([^]]+)\]\s+(.+)", definition, re.S)
            if col_match:
                column_name, fragment = col_match.group(1), col_match.group(2).strip()
                computed = re.match(r"AS\s+(.+?)(?:\s+PERSISTED(?:\s+NOT\s+NULL)?|\s+NOT\s+NULL)?$", fragment, re.I | re.S)
                source_type = "computed:" + computed.group(1).strip() if computed else type_name(fragment)
                nullable = not bool(re.search(r"\bNOT\s+NULL\b", fragment, re.I))
                if re.search(r"\b\bNULL\b", fragment, re.I) and not re.search(r"\bNOT\s+NULL\b", fragment, re.I):
                    nullable = True
                description = {
                    "name": column_name,
                    "sourceType": source_type.removeprefix("computed:"),
                    "nullable": nullable,
                    "identity": bool(re.search(r"\bIDENTITY\s*\(", fragment, re.I)),
                    "unique": bool(re.search(r"\bUNIQUE\b", fragment, re.I)),
                    "computedExpression": computed.group(1).strip() if computed else None,
                    "defaultExpression": literal_default(fragment),
                    "sourceDefinition": definition.strip(),
                }
                columns.append(description)
                continue
            check_match = re.search(r"\bCHECK\s*\(", definition, re.I)
            if check_match:
                start = check_match.end() - 1
                end = matching_paren(definition, start)
                checks.append(normalize_expression(definition[start + 1 : end]))
        tables.append({
            "schema": schema,
            "name": name,
            "recordset": f"{schema}.{name}",
            "sourceDefinition": f"CREATE TABLE [{schema}].[{name}]({raw})",
            "columns": columns,
            "checks": checks,
        })
        position = closing + 1
    if len(tables) != 71:
        raise ConversionError(f"expected 71 source tables, found {len(tables)}")
    return tables


def parse_constraints(source: str, tables: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_name = {(table["schema"], table["name"]): table for table in tables}
    primary_keys: list[dict[str, Any]] = []
    foreign_keys: list[dict[str, Any]] = []
    for block in re.finditer(r"ALTER\s+TABLE\s+\[([^]]+)\]\.\[([^]]+)\](.*?)(?=\nGO\b|\Z)", source, re.I | re.S):
        schema, table_name, body = block.groups()
        if (schema, table_name) not in by_name:
            continue
        for match in re.finditer(
            r"CONSTRAINT\s+\[([^]]+)\]\s+PRIMARY\s+KEY\s+(?:(?:CLUSTERED|NONCLUSTERED)\s*)?\(([^)]*)\)",
            body, re.I | re.S,
        ):
            constraint, columns = match.groups()
            names = re.findall(r"\[([^]]+)\]", columns)
            if not names:
                raise ConversionError(f"primary key {constraint} has no columns")
            primary_keys.append({"table": f"{schema}.{table_name}", "name": constraint, "columns": names})
        for match in re.finditer(
            r"CONSTRAINT\s+\[([^]]+)\]\s+FOREIGN\s+KEY\s*\(([^)]*)\)\s+REFERENCES\s+\[([^]]+)\]\.\[([^]]+)\]\s*\(([^)]*)\)",
            body, re.I | re.S,
        ):
            constraint, columns, target_schema, target_table, target_columns = match.groups()
            local = re.findall(r"\[([^]]+)\]", columns)
            remote = re.findall(r"\[([^]]+)\]", target_columns)
            if not local or len(local) != len(remote):
                raise ConversionError(f"foreign key {constraint} has mismatched columns")
            foreign_keys.append({
                "table": f"{schema}.{table_name}",
                "name": constraint,
                "columns": local,
                "referencedTable": f"{target_schema}.{target_table}",
                "referencedColumns": remote,
            })
    for table in tables:
        raw = table["sourceDefinition"]
        for match in re.finditer(r"\bPRIMARY\s+KEY\s*(?:CLUSTERED\s*)?\(([^)]*)\)", raw, re.I):
            cols = re.findall(r"\[([^]]+)\]", match.group(1))
            if cols:
                primary_keys.append({"table": table["recordset"], "name": "inline-primary-key", "columns": cols})
    for key in primary_keys:
        table = by_name[tuple(key["table"].split(".", 1))]
        columns = {column["name"]: column for column in table["columns"]}
        for column_name in key["columns"]:
            column = columns.get(column_name)
            if column is None:
                raise ConversionError(f"primary key {key['name']} references unknown column {key['table']}.{column_name}")
            if column["nullable"]:
                # SQL Server primary keys imply NOT NULL; SQLite table-level
                # primary keys do not consistently enforce that implication.
                column["nullable"] = False
                column["nullabilitySource"] = "primaryKey"
    if len(primary_keys) < 60 or len(foreign_keys) < 70:
        raise ConversionError(f"constraint parser found only {len(primary_keys)} primary and {len(foreign_keys)} foreign keys")
    return primary_keys, foreign_keys


def parse_bulk_imports(source: str) -> dict[str, dict[str, bytes | str]]:
    result: dict[str, dict[str, bytes | str]] = {}
    pattern = re.compile(
        r"BULK\s+INSERT\s+\[([^]]+)\]\.\[([^]]+)\]\s+FROM\s+'\$\(SqlSamplesSourceDataPath\)([^']+)'\s*WITH\s*\((.*?)\)\s*;",
        re.I | re.S,
    )
    for match in pattern.finditer(source):
        schema, table, file_name, options = match.groups()
        options_map = {key.lower(): value for key, value in re.findall(r"(FIELDTERMINATOR|ROWTERMINATOR)\s*=\s*'([^']*)'", options, re.I)}
        if set(options_map) != {"fieldterminator", "rowterminator"}:
            raise ConversionError(f"bulk import options are incomplete for {schema}.{table}")
        result[f"{schema}.{table}"] = {
            "file": file_name,
            "field": decode_terminator(options_map["fieldterminator"]),
            "row": decode_terminator(options_map["rowterminator"]),
        }
    if len(result) != 68:
        raise ConversionError(f"expected 68 bulk imports, found {len(result)}")
    return result


def decode_terminator(value: str) -> bytes:
    if value.lower().startswith("0x"):
        try:
            return bytes.fromhex(value[2:])
        except ValueError as exc:
            raise ConversionError(f"invalid hex terminator {value!r}") from exc
    return value.replace("\\t", "\t").replace("\\n", "\n").replace("\\r", "\r").encode("utf-8")


def record_rows(data: bytes, field: bytes, row: bytes, width: int, recordset: str) -> list[list[bytes]]:
    # The source's binary Document rows use Windows CRLF after the `&|` marker,
    # while the T-SQL recipe writes the row terminator as `&|\n`.
    if row == b"&|\n" and b"&|\r\n" in data:
        row = b"&|\r\n"
    if row == b"\n":
        physical_rows = data.split(row)
        if physical_rows and physical_rows[-1] == b"":
            physical_rows.pop()
        rows: list[list[bytes]] = []
        pending = bytearray()
        pending_fields = 0
        for line in physical_rows:
            if pending:
                pending.extend(b"\n")
            pending.extend(line)
            pending_fields += line.count(field)
            if pending_fields == width - 1:
                rows.append(bytes(pending).split(field))
                pending.clear()
                pending_fields = 0
            elif pending_fields > width - 1:
                raise ConversionError(f"too many fields in multiline source row for {recordset}")
        if pending or pending_fields:
            raise ConversionError(f"incomplete multiline source row for {recordset}")
    else:
        pieces = data.split(row)
        if pieces and pieces[-1] == b"":
            pieces.pop()
        rows = [piece.split(field) for piece in pieces]
    for index, values in enumerate(rows, 1):
        if len(values) != width:
            raise ConversionError(f"{recordset} source row {index} has {len(values)} fields; expected {width}")
    return rows


def value_for(raw: bytes, column: dict[str, Any]) -> Any:
    source_type = column["sourceType"].lower()
    if raw == b"":
        return None if column["nullable"] else ""
    binary = source_type in {"binary", "varbinary", "image", "timestamp", "rowversion", "geography", "geometry", "hierarchyid"}
    if binary:
        if raw[:2].lower() == b"0x":
            try:
                return bytes.fromhex(raw[2:].decode("ascii"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ConversionError(f"invalid source hex value for {column['name']}") from exc
        if re.fullmatch(rb"[0-9A-Fa-f]+", raw) and len(raw) % 2 == 0:
            return bytes.fromhex(raw.decode("ascii"))
        return raw
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ConversionError(f"non-UTF-8 source value in {column['name']}") from exc
    if source_type in {"int", "smallint", "tinyint", "bit"}:
        try:
            return int(value)
        except ValueError:
            return value
    if source_type in {"decimal", "numeric", "money", "smallmoney", "float", "real"}:
        try:
            return float(value)
        except ValueError:
            return value
    return value


def sqlite_column_sql(column: dict[str, Any]) -> str:
    source_type = column["sourceType"].lower()
    sql_type = sqlite_type(source_type if not column["computedExpression"] else "computed:" + source_type, column["name"])
    declaration = f"{quote(column['name'])} {sql_type}"
    if not column["nullable"]:
        declaration += " NOT NULL"
    default = column.get("defaultExpression")
    # SQLite only permits constant expressions in DEFAULT clauses. Keep the
    # SQL Server NEWID() default in source metadata but omit it from the static
    # read-only SQLite schema; all seeded GUID values are imported unchanged.
    if default and default.upper() != "NEWID()" and not column["computedExpression"]:
        declaration += f" DEFAULT {default}"
    return declaration


def table_sql(table: dict[str, Any], primary_keys: list[dict[str, Any]], foreign_keys: list[dict[str, Any]]) -> str:
    definitions = [sqlite_column_sql(column) for column in table["columns"]]
    for check in table["checks"]:
        definitions.append(f"CHECK ({check})")
    for item in primary_keys:
        if item["table"] == table["recordset"]:
            keys = ", ".join(quote(column) for column in item["columns"])
            definitions.append(f"CONSTRAINT {quote(item['name'])} PRIMARY KEY ({keys})")
    for item in foreign_keys:
        if item["table"] == table["recordset"]:
            local = ", ".join(quote(column) for column in item["columns"])
            remote = ", ".join(quote(column) for column in item["referencedColumns"])
            definitions.append(
                f"CONSTRAINT {quote(item['name'])} FOREIGN KEY ({local}) "
                f"REFERENCES {quote(item['referencedTable'])} ({remote})"
            )
    return f"CREATE TABLE {quote(table['recordset'])} ({', '.join(definitions)})"


def parse_views(source: str) -> list[dict[str, str]]:
    views: list[dict[str, str]] = []
    matches = list(re.finditer(r"CREATE\s+VIEW\s+\[([^]]+)\]\.\[([^]]+)\](?:\s+WITH\s+SCHEMABINDING)?\s+AS\b", source, re.I))
    for index, match in enumerate(matches):
        end = re.search(r"\nGO\b", source[match.end() :], re.I)
        if not end:
            raise ConversionError(f"cannot find GO after view {match.group(1)}.{match.group(2)}")
        sql = source[match.start() : match.end() + end.start()].strip()
        body = source[match.end() : match.end() + end.start()].strip()
        unsupported: list[str] = []
        for pattern, reason in (
            (r"\.\s*(?:value|nodes)\s*\(", "SQL Server XML methods"),
            (r"\b(?:OUTER|CROSS)\s+APPLY\b", "SQL Server APPLY operator"),
            (r"\bPIVOT\b", "SQL Server PIVOT syntax"),
        ):
            if re.search(pattern, body, re.I):
                unsupported.append(reason)
        views.append({
            "schema": match.group(1),
            "name": match.group(2),
            "recordset": f"{match.group(1)}.{match.group(2)}",
            "sourceDefinition": sql,
            "sqliteCompatibility": "unsupported" if unsupported else "unreviewed",
            "limitation": "; ".join(unsupported),
        })
    if len(views) != 20:
        raise ConversionError(f"expected 20 source views, found {len(views)}")
    return views


def sqlite_view_sql(view: dict[str, str]) -> str:
    """Translate the pinned source's plain relational view subset."""
    if view["sqliteCompatibility"] == "unsupported":
        raise ConversionError(f"view {view['recordset']} uses {view['limitation']}")
    sql = re.sub(
        r"^CREATE\s+VIEW\s+\[[^]]+\]\.\[[^]]+\](?:\s+WITH\s+SCHEMABINDING)?\s+AS\b",
        f"CREATE VIEW {quote(view['recordset'])} AS",
        view["sourceDefinition"],
        flags=re.I,
    )
    # SQL Server accepts both [schema].[table] and [schema].table forms.
    sql = re.sub(
        r"\[([^]]+)\]\.\[([^]]+)\]",
        lambda match: quote(f"{match.group(1)}.{match.group(2)}"),
        sql,
    )
    sql = re.sub(
        r"\[([^]]+)\]\.([A-Za-z_][A-Za-z0-9_]*)",
        lambda match: quote(f"{match.group(1)}.{match.group(2)}"),
        sql,
    )
    sql = quote_identifiers(sql)
    sql = re.sub(r"\bN(?=')", "", sql, flags=re.I)
    # SQL Server's SELECT alias = expression syntax occurs in three source
    # views. These expressions are simple qualified column references.
    sql = re.sub(
        r",\s*(\"[^\"]+\")\s*=\s*([A-Za-z_][A-Za-z0-9_]*\s*\.\s*\"[^\"]+\")",
        lambda match: f", {match.group(2)} AS {match.group(1)}",
        sql,
    )
    return sql


def descriptions(source: str) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    table_descriptions: dict[str, str] = {}
    column_descriptions: dict[str, dict[str, str]] = {}
    pattern = re.compile(
        r"EXECUTE\s+\[sys\]\.\[sp_addextendedproperty\]\s+N'MS_Description',\s*"
        r"N'((?:''|[^'])*)',\s*N'SCHEMA',\s*\[([^]]+)\],\s*N'TABLE',\s*\[([^]]+)\]"
        r"(?:,\s*N'COLUMN',\s*\[([^]]+)\])?",
        re.I | re.S,
    )
    for match in pattern.finditer(source):
        description, schema, table, column = match.groups()
        description = description.replace("''", "'").strip()
        recordset = f"{schema}.{table}"
        if column:
            column_descriptions.setdefault(recordset, {})[column] = description
        else:
            table_descriptions[recordset] = description
    return table_descriptions, column_descriptions


def parse_indexes(source: str, tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    table_names = {table["recordset"] for table in tables}
    indexes: list[dict[str, Any]] = []
    pattern = re.compile(
        r"CREATE\s+(?:(UNIQUE)\s+)?(?:(?:CLUSTERED|NONCLUSTERED)\s+)?INDEX\s+\[([^]]+)\]\s+ON\s+\[([^]]+)\]\.\[([^]]+)\]\s*\(([^)]*)\)",
        re.I,
    )
    for match in pattern.finditer(source):
        unique, name, schema, table, columns = match.groups()
        recordset = f"{schema}.{table}"
        if recordset not in table_names:
            continue
        indexes.append({"name": name, "table": recordset, "unique": bool(unique), "columns": re.findall(r"\[([^]]+)\]", columns)})
    for table in tables:
        for column in table["columns"]:
            if column.get("unique"):
                indexes.append({
                    "name": f"inline_unique_{column['name']}",
                    "table": table["recordset"],
                    "unique": True,
                    "columns": [column["name"]],
                    "sourceKind": "inline column UNIQUE",
                })
    return indexes


def parse_tables_and_files(source: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    tables = parse_tables(source)
    primary_keys, foreign_keys = parse_constraints(source, tables)
    bulk = parse_bulk_imports(source)
    indexes = parse_indexes(source, tables)
    table_names = {table["recordset"] for table in tables}
    if set(bulk) - table_names:
        raise ConversionError("bulk import references an unknown physical table")
    by_recordset = {table["recordset"]: table for table in tables}
    for recordset, config in bulk.items():
        file = INSTALL_DIR / str(config["file"])
        rows = record_rows(file.read_bytes(), config["field"], config["row"], len(by_recordset[recordset]["columns"]), recordset)
        config["rowCount"] = len(rows)
        config["rows"] = rows
    # The pinned SQL Server 2025 installer inserts engine properties into this
    # table. The companion CSV is its deterministic, source-pinned equivalent.
    aw_table = by_recordset["dbo.AWBuildVersion"]
    aw_file = INSTALL_DIR / "AWBuildVersion.csv"
    with aw_file.open(encoding="utf-8", newline="") as source_file:
        aw_rows = list(csv.reader(source_file, delimiter="\t"))
    if len(aw_rows) != 1 or len(aw_rows[0]) != len(aw_table["columns"]):
        raise ConversionError("AWBuildVersion.csv no longer contains one complete pinned row")
    bulk["dbo.AWBuildVersion"] = {"file": "AWBuildVersion.csv", "field": b"\t", "row": b"\n", "rowCount": 1, "rows": [list(value.encode() for value in aw_rows[0])]}
    return tables, primary_keys, foreign_keys, indexes, bulk


def build_database(path: Path, tables: list[dict[str, Any]], primary_keys: list[dict[str, Any]], foreign_keys: list[dict[str, Any]], indexes: list[dict[str, Any]], bulk: dict[str, dict[str, Any]], views: list[dict[str, str]]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=OFF")
    by_recordset = {table["recordset"]: table for table in tables}
    row_counts: dict[str, int] = {}
    try:
        for table in tables:
            db.execute(table_sql(table, primary_keys, foreign_keys))
        for recordset, config in bulk.items():
            table = by_recordset[recordset]
            columns = table["columns"]
            names = ", ".join(quote(column["name"]) for column in columns)
            placeholders = ", ".join("?" for _ in columns)
            sql = f"INSERT INTO {quote(recordset)} ({names}) VALUES ({placeholders})"
            rows = config["rows"]
            converted = [[value_for(value, column) for value, column in zip(row, columns, strict=True)] for row in rows]
            db.executemany(sql, converted)
            row_counts[recordset] = len(converted)
        for table in tables:
            row_counts.setdefault(table["recordset"], 0)
        for item in indexes:
            if not item["columns"]:
                continue
            name = f"{item['table'].replace('.', '_')}_{item['name']}"
            columns = ", ".join(quote(column) for column in item["columns"])
            unique = "UNIQUE " if item["unique"] else ""
            db.execute(f"CREATE {unique}INDEX {quote(name)} ON {quote(item['table'])} ({columns})")
        view_counts: dict[str, int] = {}
        for view in views:
            if view["sqliteCompatibility"] == "unsupported":
                continue
            view_sql = sqlite_view_sql(view)
            try:
                db.execute(view_sql)
            except sqlite3.Error as exc:
                raise ConversionError(f"relational view {view['recordset']} did not translate to SQLite: {exc}") from exc
            view["sqliteCompatibility"] = "compatible"
            view["sqliteDefinition"] = view_sql
            view["rowCount"] = db.execute(f"SELECT COUNT(*) FROM {quote(view['recordset'])}").fetchone()[0]
            view_counts[view["recordset"]] = view["rowCount"]
        db.commit()
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ConversionError(f"SQLite integrity_check failed: {integrity}")
        violations = db.execute("PRAGMA foreign_key_check").fetchmany(10)
        if violations:
            raise ConversionError(f"SQLite foreign_key_check failed: {violations!r}")
        return {"rowCounts": row_counts, "viewCounts": view_counts, "indexes": indexes, "foreignKeys": len(foreign_keys), "primaryKeys": len(primary_keys)}
    finally:
        db.close()


def write_native_metadata(path: Path, tables: list[dict[str, Any]], views: list[dict[str, Any]], primary_keys: list[dict[str, Any]], foreign_keys: list[dict[str, Any]], indexes: list[dict[str, Any]], table_descriptions: dict[str, str], column_descriptions: dict[str, dict[str, str]], row_counts: dict[str, int]) -> None:
    pks = {item["table"]: item["columns"] for item in primary_keys}
    for table in tables:
        table["description"] = table_descriptions.get(table["recordset"], "")
        table["primaryKey"] = pks.get(table["recordset"], [])
        table["rowCount"] = row_counts.get(table["recordset"], 0)
        for column in table["columns"]:
            column["description"] = column_descriptions.get(table["recordset"], {}).get(column["name"], "")
        # Runtime audit/error tables have no static CSV seed. DatabaseLog would
        # vary with installation DDL events; ErrorLog records runtime failures.
        if table["recordset"] in {"dbo.DatabaseLog", "dbo.ErrorLog"}:
            table["staticSeed"] = False
    payload = {
        "format": "demodb-native-sqlserver-metadata/draft-1",
        "upstreamRevision": EXPECTED_UPSTREAM_REVISION,
        "tableCount": len(tables),
        "viewCount": len(views),
        "tables": tables,
        "views": views,
        "primaryKeys": primary_keys,
        "foreignKeys": foreign_keys,
        "indexes": indexes,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def logical_snapshot(path: Path) -> bytes:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        schema = [tuple(row) for row in db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")]
        data: list[tuple[str, list[tuple[Any, ...]]]] = []
        for kind, name in db.execute("SELECT type,name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY type,name"):
            columns = [row[1] for row in db.execute(f"PRAGMA table_xinfo({quote(name)})")] if kind == "table" else [item[0] for item in db.execute(f"SELECT * FROM {quote(name)} LIMIT 0").description or []]
            order = ", ".join(quote(column) for column in columns)
            rows = [tuple(row) for row in db.execute(f"SELECT * FROM {quote(name)} ORDER BY {order}")]
            data.append((name, rows))
        return json.dumps({"schema": schema, "data": data}, ensure_ascii=False, sort_keys=True, default=lambda value: {"blob": value.hex()} if isinstance(value, bytes) else value).encode()
    finally:
        db.close()


def convert() -> tuple[Any, ...]:
    files = source_files()
    installer_rel = "data-source/microsoft-sql-server-samples/adventure-works/oltp-install-script/instawdb.sql"
    installer = files[installer_rel].decode("utf-8-sig", errors="strict")
    clean_source = strip_comments(installer)
    tables, primary_keys, foreign_keys, indexes, bulk = parse_tables_and_files(clean_source)
    views = parse_views(clean_source)
    table_descriptions, column_descriptions = descriptions(clean_source)
    with tempfile.TemporaryDirectory(prefix="adventureworks-convert-") as temporary:
        candidate = Path(temporary) / "source.sqlite"
        report = build_database(candidate, tables, primary_keys, foreign_keys, indexes, bulk, views)
        candidate_snapshot = logical_snapshot(candidate)
        candidate_bytes = candidate.read_bytes()
    return tables, primary_keys, foreign_keys, indexes, report, views, table_descriptions, column_descriptions, candidate_snapshot, candidate_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="rebuild in a temporary directory and compare full logical contents")
    args = parser.parse_args()
    tables, primary_keys, foreign_keys, indexes, report, views, table_descriptions, column_descriptions, candidate_snapshot, candidate_bytes = convert()
    if args.check:
        if not OUTPUT.is_file():
            raise ConversionError(f"checked-in compressed SQLite fixture is missing: {OUTPUT}")
        compressed_bytes = OUTPUT.read_bytes()
        source = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["source"]
        committed_input_digest = sha256(compressed_bytes)
        expected_input_digest = source.get("inputSha256")
        if not expected_input_digest or committed_input_digest != expected_input_digest:
            raise ConversionError(f"checked-in compressed SQLite hash mismatch: expected {expected_input_digest}, got {committed_input_digest}")
        try:
            fixture_bytes = gzip.decompress(compressed_bytes)
        except OSError as exc:
            raise ConversionError("checked-in SQLite fixture is not a valid gzip stream") from exc
        committed_digest = sha256(fixture_bytes)
        actual_digest = source["databaseSha256"]
        if committed_digest != actual_digest:
            raise ConversionError(f"decompressed source SQLite hash mismatch: expected {actual_digest}, got {committed_digest}")
        with tempfile.TemporaryDirectory(prefix="adventureworks-fixture-check-") as temporary:
            fixture = Path(temporary) / "source.sqlite"
            fixture.write_bytes(fixture_bytes)
            if candidate_snapshot != logical_snapshot(fixture):
                raise ConversionError("temporary rebuild differs from checked-in SQLite schema or row data")
            native_metadata = Path(temporary) / "native-objects.json"
            write_native_metadata(
                native_metadata,
                tables,
                views,
                primary_keys,
                foreign_keys,
                indexes,
                table_descriptions,
                column_descriptions,
                report["rowCounts"],
            )
            if not NATIVE_METADATA.is_file() or native_metadata.read_bytes() != NATIVE_METADATA.read_bytes():
                raise ConversionError("checked-in native SQL Server metadata differs from the pinned installer and CSV source")
        print(f"AdventureWorks logical source check passed: {len(tables)} tables, {len(views)} original views ({len(report['viewCounts'])} SQLite-compatible), {sum(report['rowCounts'].values()):,} table rows")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0, compresslevel=9) as compressed:
            compressed.write(candidate_bytes)
    write_native_metadata(NATIVE_METADATA, tables, views, primary_keys, foreign_keys, indexes, table_descriptions, column_descriptions, report["rowCounts"])
    source = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["source"]
    digest = sha256(candidate_bytes)
    compressed_digest = sha256(OUTPUT.read_bytes())
    if source.get("databaseSha256") != digest:
        raise ConversionError(f"manifest source.databaseSha256 must be updated to {digest}")
    if source.get("inputSha256") != compressed_digest:
        raise ConversionError(f"manifest source.inputSha256 must be updated to {compressed_digest}")
    print(f"Built {len(tables)} AdventureWorks tables and {sum(report['rowCounts'].values()):,} rows; SQLite SHA-256 {digest}; gzip SHA-256 {compressed_digest}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConversionError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        raise SystemExit(f"error: {error}")
