"""Asynchronous document processing (M3): parse jobs, dispatch, recovery, reconciliation.

See docs/document-processing.md. Module map:

- jobs.py          the job state machine in SQL: claim, fencing, completion, retry, recovery
- dispatch.py      bounded broker publishing and the dispatch sweep over the job table (outbox)
- extraction.py    runs extract_child.py in a resource-limited subprocess, validates its output
- extract_child.py the only code that opens a PDF with pypdf (self-contained script)
- normalize.py     conservative text normalization and page -> section mapping
- pipeline.py      one parse attempt from claim to completion, and failure classification
- reconcile.py     file reconciliation (temporary-file cleanup and reports)
- runtime.py       per-process worker context and start-up checks
- admin.py         operator commands (preflight, init-storage, retry-parse)

Nothing here imports pypdf: the API process never loads the PDF parser.
"""
