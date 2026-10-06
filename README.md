# AdventureWorks OLTP for DemoDB

This repository prepares Microsoft's AdventureWorks OLTP sample as a reproducible SQLite fixture. The pinned source is the AdventureWorks OLTP installer at Microsoft SQL Server Samples commit `beaab06ef72831089ca80e5355d65e661fd19b26`, with the matching 2025 build row `17.0.1000.3`. It preserves 71 physical tables, 759,240 seeded rows, the source primary and foreign keys, and 93 explicit indexes plus the source inline uniqueness constraint.

Eleven source views have compatible relational definitions that are translated to SQLite. The other nine SQL Server views use XML methods, `APPLY`, or `PIVOT`; their original definitions remain in both the pinned native-object metadata and the generated `metadata/schema.json` `sourceViews` list, marked as unavailable in SQLite. The executable view list remains distinct from these retained source definitions. `DatabaseLog` and `ErrorLog` are empty because the installer populates them only with runtime audit and error events. No rows are fabricated for these tables or views.

The source and conversion details are documented in [`data-source/README.md`](data-source/README.md). Microsoft's MIT license is included in [`LICENSE`](LICENSE). SQL Server `GETDATE()` defaults are frozen to the pinned build timestamp. Native `NEWID()` defaults remain documented in source metadata but are omitted from static SQLite DDL; source-seeded GUID values are preserved. The verified read-only OVDB mount provides record lookups and query access; writes remain disabled.

The pinned installer and CSV inputs are kept byte-for-byte, and generated CSV exports retain source text values exactly. A few such files contain CRLF or trailing tabs/spaces, so `.gitattributes` scopes Git's diff-whitespace exceptions to only those source and generated data files; code, metadata, and documentation remain covered by the normal whitespace checks. Source hashes and logical rebuild checks protect those preserved bytes and values.

## Native inGitDB snapshot

The `ingitdb/` directory contains 759,240 source table rows across 71 collections. It is a Git-backed, queryable snapshot prepared from the pinned SQLite fixture. Verify and query it with the installed inGitDB CLI:

```sh
ingitdb validate --path ingitdb
ingitdb select --path ingitdb --from humanresources_department_3ff8b674 --limit 1 --format json
```

[`ingitdb/export-manifest.json`](ingitdb/export-manifest.json) maps each native table to its collection, row count, original primary and foreign keys, column types, transport encodings, and SHA-256 of its record file. The source fixture SHA-256 is `6a105e1982becfe003fc7a307d7cad9d738390cedd79d166c2167fd817168109`. These bytes were exported against provider commit `5028a27189b487d6fd8025fafc1307aada707fd2`; the source fixture hash also matches this repository's pinned fixture. Record keys encode native primary keys where present; keyless tables use stable ordinal IDs, which are not native keys. Native `primary_key` names the source key columns while encoded record IDs remain the transport keys. inGitDB validates safe transported column types and required fields; `exportRequired` can be stricter than SQLite declaration for nullable primary keys. The export manifest preserves source SQL, ordered indexes and foreign-key groups with actions, defaults, and declared nullability. Foreign keys and SQL uniqueness, CHECK, collation, default, and action behavior are source metadata here, not constraints enforced by inGitDB. Exact decimal values travel as strings and binary values as base64 where marked in column metadata. Source view definitions are retained as metadata only; they are not materialized in inGitDB. Source rights and original notices remain in [`data-source/`](data-source/) and [`LICENSE`](LICENSE).
