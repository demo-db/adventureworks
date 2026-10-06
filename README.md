# AdventureWorks OLTP for DemoDB

This repository prepares Microsoft's AdventureWorks OLTP sample as a reproducible SQLite fixture. The pinned source is the AdventureWorks OLTP installer at Microsoft SQL Server Samples commit `beaab06ef72831089ca80e5355d65e661fd19b26`, with the matching 2025 build row `17.0.1000.3`. It preserves 71 physical tables, 759,240 seeded rows, the source primary and foreign keys, and 93 explicit indexes plus the source inline uniqueness constraint.

Eleven source views have compatible relational definitions that are translated to SQLite. The other nine SQL Server views use XML methods, `APPLY`, or `PIVOT`; their original definitions remain in both the pinned native-object metadata and the generated `metadata/schema.json` `sourceViews` list, marked as unavailable in SQLite. The executable view list remains distinct from these retained source definitions. `DatabaseLog` and `ErrorLog` are empty because the installer populates them only with runtime audit and error events. No rows are fabricated for these tables or views.

The source and conversion details are documented in [`data-source/README.md`](data-source/README.md). Microsoft's MIT license is included in [`LICENSE`](LICENSE). SQL Server `GETDATE()` defaults are frozen to the pinned build timestamp. Native `NEWID()` defaults remain documented in source metadata but are omitted from static SQLite DDL; source-seeded GUID values are preserved. The verified read-only OVDB mount provides record lookups and query access; writes remain disabled.

The pinned installer and CSV inputs are kept byte-for-byte, and generated CSV exports retain source text values exactly. A few such files contain CRLF or trailing tabs/spaces, so `.gitattributes` scopes Git's diff-whitespace exceptions to only those source and generated data files; code, metadata, and documentation remain covered by the normal whitespace checks. Source hashes and logical rebuild checks protect those preserved bytes and values.

## Native inGitDB snapshot

The `ingitdb/` directory contains 759,240 source rows in 71 collections, exported from the pinned SQLite fixture by DataTug's generic DALgo → inGitDB exporter. The source fixture SHA-256 is `6a105e1982becfe003fc7a307d7cad9d738390cedd79d166c2167fd817168109`. This Git-backed edition is a queryable snapshot, not a live SQL database.

Use DataTug CLI v0.61.1 or newer to reproduce this export, and inGitDB CLI v0.70.0 or newer to validate and query this edition.

```sh
ingitdb validate --path ingitdb
ingitdb select --path ingitdb --from 'HumanResources.Department' --limit 1 --format json
```

Each source table has a `.collection/definition.yaml` with ordered fields, source primary-key columns, portable indexes and foreign-key groups/actions. `.ingitdb/source-collections.json` maps native collection IDs to exact SQLite table names; names outside inGitDB’s ID alphabet use a deterministic `dt_` UTF-8 hex ID. Its `source_schema.source_definition_json` retains the original SQLite DDL, declared column types, defaults and complete index details. The native record file is `records.json`, keyed by deterministic transport IDs derived from the ordered source primary key; keyless tables use source-row ordinals. These transport IDs are not new SQL columns. Exact decimals are stored as strings, BLOBs as base64, and `source-storage-*.jsonl` sidecars retain decimal SQLite storage classes where needed. The 11 source view definitions remain in `.ingitdb/source-views.yaml` as metadata; they are not materialized collections.

The checked-in Git snapshot is the published inGitDB edition. [`ingitdb/export-manifest.json`](ingitdb/export-manifest.json) records the DataTug version, binary hash, pinned source and record checksums, plus the independent parity receipt at [`ingitdb/native-parity-report.json`](ingitdb/native-parity-report.json). Its `prepared-not-hosted` status describes the generated bundle before repository publication and also covers BigQuery load files; it does not imply a hosted BigQuery service.

The published record format is DataTug's default JSON. To produce another edition from a verified, decoded copy of this pinned SQLite fixture, choose a **new** destination and pass `--records-format json` (default), `jsonl`, `ingr`, `csv`, or `yaml`:

```sh
datatug db export --from sqlite:///absolute/path/to/pinned-source.sqlite \
  --to ingitdb:///absolute/path/to/new-output --records-format json
```

The independent checker in `demo-db/websites/scripts/hosting-tools/validate_datatug_exports.py` compares the native schema and every typed row at its transport ID with this repository's pinned source. Run it from a checkout containing both repositories:

```sh
python3 ../websites/scripts/hosting-tools/validate_datatug_exports.py . ingitdb \
  --report /private/tmp/adventureworks-ingitdb-parity.json
```

The source primary keys, foreign keys, UNIQUE and CHECK constraints, defaults, collations and SQL actions are preserved as source metadata; inGitDB does not enforce their full SQL behavior on later record edits. Source rights and original notices remain in [`data-source/`](data-source/) and [`LICENSE`](LICENSE).
