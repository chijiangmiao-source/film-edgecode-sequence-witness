"""Shared pytest setup.

The module-level ``app`` in ``app.main`` opens its ingestion SQLite file on
import; point it at a throwaway directory so test runs never leave database
files in the repository.  Tests that exercise ingestion create their own
apps on ``tmp_path`` databases and are unaffected by this default.
"""

from __future__ import annotations

import os
import tempfile

os.environ["EDGECODE_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="edgecode-test-"), "ingest.db"
)
