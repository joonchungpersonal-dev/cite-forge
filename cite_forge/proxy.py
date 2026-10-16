"""UM Library Proxy URL generation (A25-Manual variant).

============================================================================
COMPLIANCE BOUNDARY — READ BEFORE MODIFYING
============================================================================

This module generates CLICKABLE URLs for the UM Library proxy. It does NOT
authenticate, NOT fetch, NOT batch-download, NOT scrape. It produces a URL
that the user opens in their browser; Shibboleth handles auth interactively;
the user downloads the PDF manually.

This is the ONLY UM-proxy interaction cite-forge is permitted to perform.

University of Miami Libraries' "Acceptable Use of Electronic Resources
Policy" explicitly lists as NOT ACCEPTABLE:

    "Systematic or substantial printing, copying or downloading
     (such as entire journal issues or books)"

And consequences:

    "If license terms are violated, licensors usually have the right to
     temporarily suspend access FOR THE ENTIRE UNIVERSITY COMMUNITY. In
     some cases, licenses can be permanently revoked."

Source: https://www.library.miami.edu/about/electronic-resources.html

Elsevier's TDM license further prohibits "robots, spiders or other automated
downloading programs" outside their licensed TDM API channel.

Therefore this module:
  - Generates proxy URLs only. ✓
  - Never stores credentials. ✓
  - Never sends HTTP requests to access.library.miami.edu. ✓
  - Never persists session cookies. ✓
  - Never instructs the user to bulk-download. ✓

If a future contributor adds a cookie jar, automated login, or batch fetch
to this module: that violates UM AUP and risks suspending UM-wide access.
DON'T.

Legitimate extension paths if more automation is needed:
  - Publisher TDM APIs (Elsevier dev portal, Springer TDM, Wiley TDM) with
    user-registered API key — see `cite_forge.tdm` (not yet implemented).
  - Inter-library loan (DOCLINE/ILLiad) via A26 — also manual at the
    request level.

============================================================================
"""

from __future__ import annotations

import re
import urllib.parse

UM_PROXY_LOGIN = "https://login.access.library.miami.edu/login"

DOI_REGEX = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Za-z0-9%]+$")


def um_proxy_url(target_url: str) -> str:
    """Wrap any URL in the UM library proxy login URL.

    Returns a URL that, when opened in a browser, prompts UM Shibboleth
    authentication and then redirects to `target_url` with the proxy in
    front, granting access to UM-subscribed content.

    The user clicks this once per citation. No automation.
    """
    if not target_url:
        raise ValueError("target_url required")
    return f"{UM_PROXY_LOGIN}?url={urllib.parse.quote(target_url, safe=':/?#&=%')}"


def doi_url(doi: str) -> str:
    """Canonical resolver URL for a DOI."""
    if not DOI_REGEX.match(doi):
        raise ValueError(f"invalid DOI: {doi!r}")
    return f"https://doi.org/{doi}"


def um_proxy_for_doi(doi: str) -> str:
    """Convenience: UM proxy → DOI resolver. Publisher handles the redirect."""
    return um_proxy_url(doi_url(doi))


def render_missing_md_entry(
    *,
    ref_num: int,
    label: str,
    doi: str | None,
    publisher: str | None = None,
) -> str:
    """Markdown entry for MISSING.md.

    Example output:
        | 3 | Baglioni 2011 J Affect Disord | [Click to fetch via UM proxy](https://login.access.library.miami.edu/login?url=https%3A%2F%2Fdoi.org%2F10.1016%2Fj.jad.2011.01.011) | Elsevier |
    """
    if doi:
        url = um_proxy_for_doi(doi)
        link_md = f"[click to fetch via UM proxy]({url})"
    else:
        link_md = "no DOI — manual lookup needed"
    publisher_md = publisher or "—"
    return f"| {ref_num} | {label} | {link_md} | {publisher_md} |"


def render_missing_md_block(
    citations: list[dict],
) -> str:
    """Render a full MISSING.md table block.

    Each `citations` entry is a dict with keys: ref_num, label, doi, publisher.
    """
    header = (
        "| Ref | Citation | UM proxy link (click to authenticate + fetch) | Publisher |\n"
        "|----:|----------|----------------------------------------------|-----------|"
    )
    rows = [
        render_missing_md_entry(
            ref_num=c["ref_num"],
            label=c["label"],
            doi=c.get("doi"),
            publisher=c.get("publisher"),
        )
        for c in citations
    ]
    return header + "\n" + "\n".join(rows)
