# SPDX-License-Identifier: Apache-2.0
"""Oaken Core — the product bundle.

Installing ``oaken-core`` brings in the whole non-UI product (ADR 0014): the ingestion and
enrichment pipeline and the local API daemon (``oaken``, with the first-party Gmail, IMAP, SMTP,
Ollama, enricher and vector-store plugins), the MCP server that lets an AI work with your mail
(hosted in core, at ``/mcp``), and the ``oaken`` command-line interface (``oaken-cli``) that starts,
configures and drives it.

This package ships no code of its own; it exists so ``pip install oaken-core`` installs the bundle.
Its version is the product's single source of truth, ``oaken.__version__`` (all four distributions
read it), so there is no version literal here to drift.
"""
