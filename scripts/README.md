# Scripts Documentation

## check_migrations.py

Applies the whole migration chain to a throwaway namespace and rolls it back, as
a pre-flight before a migration reaches a real database.

### Why It Exists

Asserting on migration *text* cannot catch a SurrealQL syntax error - only
executing the migration can. Migration 25 shipped a multi-table relation
declaration (`TYPE RELATION IN note, memory_item OUT source, ...`) that SurrealDB
v2 rejects with a parse error. Every unit test was green; the failure only
appeared when the image ran against a real database, where it aborted the
migration and (because the API refuses to start afterwards) took the deployment
down.

### Usage

```bash
# Apply every migration to an empty schema, then roll back the last one
uv run python scripts/check_migrations.py

# Also exercise the whole down chain
uv run python scripts/check_migrations.py --all-down

# Keep the probe schema for inspection
uv run python scripts/check_migrations.py --keep
```

It reads the same `SURREAL_*` variables the app does (loading `.env` from the
repo root when present) and refuses to run against the namespace that
environment is configured for, so it can never modify real data. Exit code is
non-zero when the chain fails to apply or fails to roll back.

## export_docs.py

Consolidates markdown documentation files for use with ChatGPT or other platforms with file upload limits.

### What It Does

- Scans all subdirectories in the `docs/` folder
- For each subdirectory, combines all `.md` files (excluding `index.md` files)
- Creates one consolidated markdown file per subdirectory
- Saves all exported files to `doc_exports/` in the project root

### Usage

```bash
# Using Makefile (recommended)
make export-docs

# Or run directly with uv
uv run python scripts/export_docs.py

# Or run with standard Python
python scripts/export_docs.py
```

### Output

The script creates `doc_exports/` directory with consolidated files like:

- `getting-started.md` - All getting-started documentation
- `user-guide.md` - All user guide content
- `features.md` - All feature documentation
- `development.md` - All development documentation
- etc.

Each exported file includes:
- A main header with the folder name
- Section headers for each source file
- Source file attribution
- The complete content from each markdown file
- Visual separators between sections

### Example Output Structure

```markdown
# Getting Started

This document consolidates all content from the getting-started documentation folder.

---

## Installation

*Source: installation.md*

[Full content of installation.md]

---

## Quick Start

*Source: quick-start.md*

[Full content of quick-start.md]

---
```

### Notes

- The `doc_exports/` directory is gitignored and safe to regenerate anytime
- Index files (`index.md`) are automatically excluded
- Files are sorted alphabetically for consistent output
- The script handles subdirectories only (ignores files in the root `docs/` folder)
