# SPDX-License-Identifier: Apache-2.0
"""The derived search index (``index.db``): documents, chunks and FTS5, rebuildable from raw.db."""

from oaken.index.analyze import Analysis, analyze
from oaken.index.query import ParsedQuery, parse_query
from oaken.index.service import (
    REBUILD_KIND,
    Indexer,
    RebuildJobResult,
    RebuildPayload,
    RebuildResult,
    enqueue_rebuild,
    rebuild_index,
    register_index_jobs,
)
from oaken.index.store import (
    HL_CLOSE,
    HL_OPEN,
    PIPELINE_VERSION,
    IndexResult,
    IndexStats,
    IndexStore,
    SearchFilters,
    SearchHit,
    SearchResult,
    render_snippet,
)

__all__ = [
    "HL_CLOSE",
    "HL_OPEN",
    "PIPELINE_VERSION",
    "REBUILD_KIND",
    "Analysis",
    "IndexResult",
    "IndexStats",
    "IndexStore",
    "Indexer",
    "ParsedQuery",
    "RebuildJobResult",
    "RebuildPayload",
    "RebuildResult",
    "SearchFilters",
    "SearchHit",
    "SearchResult",
    "analyze",
    "enqueue_rebuild",
    "parse_query",
    "rebuild_index",
    "register_index_jobs",
    "render_snippet",
]
