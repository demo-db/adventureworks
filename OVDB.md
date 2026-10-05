---
ovdb: 1
publish:
  - ./ovdb.yaml
  - ./ovdb-database.json
---

# OpenVaultDB publication

This repository publishes the AdventureWorks OLTP database manifest and provider descriptor. The public database identity is `https://demodb.dev/adventureworks/`; its descriptor is also served from the shared server at `https://demodb.dev/ovdb/db/adventureworks/ovdb-database.json`.

The provider retains the complete source database and native table names. The generated static SQLite export uses the shared provider generator's verified gzip chunk format. The verified read-only OVDB mount provides record lookups and query access; writes remain disabled.
