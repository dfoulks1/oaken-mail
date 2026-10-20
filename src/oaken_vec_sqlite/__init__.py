# SPDX-License-Identifier: Apache-2.0
"""``oaken-vec-sqlite``: the first-party vector store, built on sqlite-vec.

Registered in the ``oaken.vector_stores`` entry-point group like any other plugin. Core never
imports this package; it is chosen with ``[ai] vector_store = "sqlite-vec"``.
"""

from oaken_vec_sqlite.store import ModelInfo, ReembedPlan, SqliteVecStore, UpsertResult

__all__ = ["ModelInfo", "ReembedPlan", "SqliteVecStore", "UpsertResult"]
