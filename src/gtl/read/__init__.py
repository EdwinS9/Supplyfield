"""Readers (lane C): files to claims."""
from __future__ import annotations

from gtl.read.document_reader import DocumentReaderAgent
from gtl.read.table_reader import TableReader, promote_mappings, read_table

# The table reader takes csv, tsv, json, xlsx, xlsm and md; the document reader takes pdf, jpg, jpeg
# and png. No file extension is in both lists.
AGENTS = [TableReader(), DocumentReaderAgent()]

__all__ = ["AGENTS", "DocumentReaderAgent", "TableReader", "promote_mappings", "read_table"]
