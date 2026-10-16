"""Retrieval source modules.

Each module exposes a uniform interface:

    async def lookup(identity: CitationIdentity) -> SourceResult

`SourceResult` carries the PDF bytes (or None), source URL, license, and
retrieval metadata. The Retrieval Coordinator (A19) consumes these.
"""
