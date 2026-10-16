"""W2 — Identity Resolution (parallel per citation).

Six agents query independent identifier authorities. They do NOT see each
other's outputs.

A05 CrossRef
A06 PubMed (NCBI E-utilities)
A07 OpenAlex
A08 Preprint Server (Crossref relations + arXiv/bioRxiv/medRxiv search)
A09 Semantic Scholar
A10 ORCID Resolution
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from cite_forge.agents.base import APIAgent
from cite_forge.http import cached_get, cached_get_json
from cite_forge.schemas import Bibliography

DOI_REGEX = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Za-z0-9%]+$")
PMID_REGEX = re.compile(r"^\d{1,9}$")
PMCID_REGEX = re.compile(r"^PMC\d+$")
OPENALEX_REGEX = re.compile(r"^W\d+$")
ORCID_REGEX = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
ARXIV_REGEX = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$|^[a-z\-]+/\d{7}$")
BIORXIV_REGEX = re.compile(r"^10\.1101/\d{4}\.\d{2}\.\d{2}\.\d+$")


def _orcid_checksum(orcid: str) -> bool:
    digits = orcid.replace("-", "")
    if len(digits) != 16:
        return False
    total = 0
    for ch in digits[:-1]:
        if not ch.isdigit():
            return False
        total = (total + int(ch)) * 2
    remainder = total % 11
    check = (12 - remainder) % 11
    expected = "X" if check == 10 else str(check)
    return expected == digits[-1]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IdentityQueryInput(_Frozen):
    ref_num: int = Field(ge=1)
    bibliography: Bibliography
    declared_doi: str | None = None


# ---------- A05: CrossRef ----------


class CrossrefCandidate(_Frozen):
    doi: str
    title: str
    first_author_family: str
    year: int
    journal: str | None = None
    score: float = Field(ge=0.0)


class A05Output(_Frozen):
    candidates: list[CrossrefCandidate]
    selected_idx: int | None
    source: str = "crossref"


class DOIResolutionAgent(APIAgent[IdentityQueryInput, A05Output]):
    name = "A05_doi_resolution"
    wave = "W2"

    BASE = "https://api.crossref.org/works"

    def _execute(self, payload: IdentityQueryInput) -> A05Output:
        # Prefer DOI lookup if declared
        if payload.declared_doi and DOI_REGEX.match(payload.declared_doi):
            doi = payload.declared_doi
            status, data = cached_get_json(
                "crossref",
                "doi",
                doi,
                f"{self.BASE}/{urllib.parse.quote(doi, safe='')}",
            )
            if status == 200 and data and "message" in data:
                msg = data["message"]
                cand = _crossref_msg_to_candidate(msg)
                if cand:
                    return A05Output(candidates=[cand], selected_idx=0)

        bib = payload.bibliography
        first_family = bib.authors[0].family if bib.authors else ""
        query_bib = f"{bib.title} {first_family} {bib.year}".strip()
        params = {
            "query.bibliographic": bib.title,
            "query.author": first_family,
            "rows": 5,
            "select": "DOI,title,author,issued,container-title,score",
        }
        ident = f"bib:{hash(query_bib) & 0xffffffff:08x}"
        status, data = cached_get_json(
            "crossref", "search", ident, self.BASE, params=params
        )
        if status != 200 or not data or "message" not in data:
            return A05Output(candidates=[], selected_idx=None)

        candidates: list[CrossrefCandidate] = []
        for item in data["message"].get("items", []):
            cand = _crossref_msg_to_candidate(item)
            if cand:
                candidates.append(cand)

        if not candidates:
            return A05Output(candidates=[], selected_idx=None)

        # Pick best: match year + first author family
        expected_family = first_family.lower()
        expected_year = bib.year
        best_idx = None
        best_key: tuple = (1e9, -1.0)
        for i, c in enumerate(candidates):
            from rapidfuzz.distance import Levenshtein

            d = Levenshtein.distance(c.first_author_family.lower(), expected_family)
            year_match = 0 if c.year == expected_year else abs(c.year - expected_year)
            key = (d + year_match * 3, -c.score)
            if key < best_key:
                best_key = key
                best_idx = i

        # Sanity-gate the selection — if author distance > 2 or year off by > 1,
        # don't select (verifier would reject anyway).
        if best_idx is not None:
            sel = candidates[best_idx]
            from rapidfuzz.distance import Levenshtein

            if Levenshtein.distance(
                sel.first_author_family.lower(), expected_family
            ) > 2 or sel.year != expected_year:
                best_idx = None

        return A05Output(candidates=candidates, selected_idx=best_idx)

    def _verify(self, payload: IdentityQueryInput, output: A05Output) -> None:
        if output.selected_idx is None:
            if output.candidates and False:  # candidates without selection is allowed
                self.fail("candidates present but selected_idx is None")
            return
        if not (0 <= output.selected_idx < len(output.candidates)):
            self.fail("selected_idx out of bounds")
        sel = output.candidates[output.selected_idx]
        if not DOI_REGEX.match(sel.doi):
            self.fail(f"selected DOI {sel.doi!r} fails regex")
        from rapidfuzz.distance import Levenshtein

        expected_family = payload.bibliography.authors[0].family
        d = Levenshtein.distance(sel.first_author_family.lower(), expected_family.lower())
        if d > 2:
            self.fail(
                f"selected first-author Levenshtein={d} > 2 "
                f"({sel.first_author_family!r} vs {expected_family!r})"
            )
        if sel.year != payload.bibliography.year:
            self.fail(f"selected year {sel.year} != expected {payload.bibliography.year}")


def _crossref_msg_to_candidate(msg: dict) -> CrossrefCandidate | None:
    doi = (msg.get("DOI") or "").lower()
    if not DOI_REGEX.match(doi):
        return None
    title_list = msg.get("title") or []
    title = title_list[0] if title_list else ""
    authors = msg.get("author") or []
    family = authors[0].get("family", "") if authors else ""
    issued = (msg.get("issued") or {}).get("date-parts") or [[None]]
    year = issued[0][0] if issued and issued[0] else None
    if not isinstance(year, int):
        return None
    journal_list = msg.get("container-title") or []
    journal = journal_list[0] if journal_list else None
    score = float(msg.get("score") or 0.0)
    return CrossrefCandidate(
        doi=doi,
        title=title,
        first_author_family=family,
        year=year,
        journal=journal,
        score=score,
    )


# ---------- A06: PubMed ----------


class A06Output(_Frozen):
    pmid: str | None
    pmcid: str | None
    citation_status: str | None
    mesh_terms: list[str] = Field(default_factory=list)
    retraction_links: list[str] = Field(default_factory=list)
    source: str = "pubmed"


class PubMedAgent(APIAgent[IdentityQueryInput, A06Output]):
    name = "A06_pubmed"
    wave = "W2"

    BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def _execute(self, payload: IdentityQueryInput) -> A06Output:
        bib = payload.bibliography
        first_family = bib.authors[0].family if bib.authors else ""

        # Step 1: esearch — find PMID by title + author + year, or by DOI
        if payload.declared_doi and DOI_REGEX.match(payload.declared_doi):
            term = f"{payload.declared_doi}[DOI]"
            ident = f"doi:{payload.declared_doi}"
        else:
            title_short = " ".join(bib.title.split()[:8])
            term = f"{title_short}[Title] AND {first_family}[Author] AND {bib.year}[PDAT]"
            ident = f"q:{hash(term) & 0xffffffff:08x}"

        params = {"db": "pubmed", "term": term, "retmode": "json", "retmax": 5}
        status, data = cached_get_json(
            "pubmed", "esearch", ident, f"{self.BASE}/esearch.fcgi", params=params
        )
        if status != 200 or not data:
            return A06Output(pmid=None, pmcid=None, citation_status=None)

        idlist = (data.get("esearchresult") or {}).get("idlist") or []
        if not idlist:
            # Relax: title-only
            params = {
                "db": "pubmed",
                "term": f"{' '.join(bib.title.split()[:10])}[Title]",
                "retmode": "json",
                "retmax": 5,
            }
            status, data = cached_get_json(
                "pubmed", "esearch_relax", ident, f"{self.BASE}/esearch.fcgi", params=params
            )
            idlist = (
                (data.get("esearchresult") or {}).get("idlist") if data else []
            ) or []

        if not idlist:
            return A06Output(pmid=None, pmcid=None, citation_status=None)

        # Step 2: efetch XML for the top hit
        pmid = idlist[0]
        params = {"db": "pubmed", "id": pmid, "retmode": "xml"}
        status, body, _ = cached_get(
            "pubmed", "efetch", pmid, f"{self.BASE}/efetch.fcgi", params=params
        )
        pmcid = None
        mesh: list[str] = []
        retract_links: list[str] = []
        status_str = None
        if status == 200 and body:
            try:
                root = ET.fromstring(body)
                for art_id in root.iter("ArticleId"):
                    if art_id.get("IdType") == "pmc":
                        v = art_id.text or ""
                        if v.startswith("PMC"):
                            pmcid = v
                        elif v.isdigit():
                            pmcid = f"PMC{v}"
                for desc in root.iter("DescriptorName"):
                    if desc.text:
                        mesh.append(desc.text)
                # Retraction linkages
                for rt in root.iter("CommentsCorrections"):
                    rtype = rt.get("RefType") or ""
                    if "Retraction" in rtype or "Erratum" in rtype:
                        ref_pmid = rt.find("PMID")
                        if ref_pmid is not None and ref_pmid.text:
                            retract_links.append(f"{rtype}:PMID{ref_pmid.text}")
                pub_status = root.find(".//PubmedData/PublicationStatus")
                if pub_status is not None:
                    status_str = pub_status.text
            except ET.ParseError:
                pass

        return A06Output(
            pmid=pmid,
            pmcid=pmcid,
            citation_status=status_str,
            mesh_terms=mesh,
            retraction_links=retract_links,
        )

    def _verify(self, payload: IdentityQueryInput, output: A06Output) -> None:
        if output.pmid is not None and not PMID_REGEX.match(output.pmid):
            self.fail(f"PMID {output.pmid!r} fails regex")
        if output.pmcid is not None and not PMCID_REGEX.match(output.pmcid):
            self.fail(f"PMCID {output.pmcid!r} fails regex")


# ---------- A07: OpenAlex ----------


class A07Output(_Frozen):
    openalex_id: str | None
    doi: str | None
    pmid: str | None
    pmcid: str | None
    arxiv_id: str | None
    concepts: list[str] = Field(default_factory=list)
    oa_locations: list[str] = Field(default_factory=list)
    host_type_per_location: list[str] = Field(default_factory=list)
    source: str = "openalex"


class OpenAlexAgent(APIAgent[IdentityQueryInput, A07Output]):
    name = "A07_openalex"
    wave = "W2"

    BASE = "https://api.openalex.org/works"

    def _execute(self, payload: IdentityQueryInput) -> A07Output:
        # Prefer DOI lookup
        if payload.declared_doi and DOI_REGEX.match(payload.declared_doi):
            ident = f"doi:{payload.declared_doi}"
            url = f"{self.BASE}/https://doi.org/{payload.declared_doi}"
            status, data = cached_get_json("openalex", "doi", ident, url)
            if status == 200 and data:
                return _openalex_msg_to_output(data)

        bib = payload.bibliography
        first_family = bib.authors[0].family if bib.authors else ""
        params = {
            "search": bib.title,
            "per-page": 5,
            "filter": f"publication_year:{bib.year}",
        }
        ident = f"search:{hash(bib.title) & 0xffffffff:08x}:{bib.year}"
        status, data = cached_get_json("openalex", "search", ident, self.BASE, params=params)
        if status != 200 or not data:
            return A07Output(
                openalex_id=None,
                doi=None,
                pmid=None,
                pmcid=None,
                arxiv_id=None,
            )
        for w in data.get("results", []):
            authors = w.get("authorships") or []
            top_family = ""
            if authors:
                top_family = (
                    (authors[0].get("author") or {}).get("display_name") or ""
                ).split()[-1]
            from rapidfuzz.distance import Levenshtein

            if (
                Levenshtein.distance(top_family.lower(), first_family.lower()) <= 2
                and w.get("publication_year") == bib.year
            ):
                return _openalex_msg_to_output(w)
        return A07Output(
            openalex_id=None, doi=None, pmid=None, pmcid=None, arxiv_id=None
        )

    def _verify(self, payload: IdentityQueryInput, output: A07Output) -> None:
        if output.openalex_id is not None and not OPENALEX_REGEX.match(output.openalex_id):
            self.fail(f"OpenAlex ID {output.openalex_id!r} fails regex")
        if len(output.host_type_per_location) != len(output.oa_locations):
            self.fail("host_type_per_location and oa_locations length mismatch")


def _openalex_msg_to_output(w: dict) -> A07Output:
    oa_id = (w.get("id") or "").split("/")[-1] or None
    doi = (w.get("doi") or "").replace("https://doi.org/", "").lower() or None
    ids = w.get("ids") or {}
    pmid_url = ids.get("pmid") or ""
    pmid = pmid_url.rsplit("/", 1)[-1] if pmid_url else None
    pmcid_url = ids.get("pmcid") or ""
    pmcid_part = pmcid_url.rsplit("/", 1)[-1] if pmcid_url else None
    pmcid = (
        pmcid_part
        if pmcid_part and pmcid_part.startswith("PMC")
        else (f"PMC{pmcid_part}" if pmcid_part and pmcid_part.isdigit() else None)
    )
    concepts = [
        c.get("display_name") for c in (w.get("concepts") or []) if c.get("display_name")
    ]
    oa_locs: list[str] = []
    host_types: list[str] = []
    for loc in w.get("locations") or []:
        url = loc.get("pdf_url") or loc.get("landing_page_url")
        if url:
            oa_locs.append(url)
            src = loc.get("source") or {}
            host_types.append(src.get("type") or "")
    return A07Output(
        openalex_id=oa_id,
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
        arxiv_id=None,
        concepts=concepts,
        oa_locations=oa_locs,
        host_type_per_location=host_types,
    )


# ---------- A08: Preprint Server ----------


class PreprintHit(_Frozen):
    server: str
    preprint_id: str
    version_count: int = Field(ge=1)
    posted: str
    vor_doi: str | None


class A08Output(_Frozen):
    hits: list[PreprintHit]
    source: str = "preprint_servers"


class PreprintServerAgent(APIAgent[IdentityQueryInput, A08Output]):
    name = "A08_preprint_servers"
    wave = "W2"

    def _execute(self, payload: IdentityQueryInput) -> A08Output:
        bib = payload.bibliography
        hits: list[PreprintHit] = []

        # bioRxiv / medRxiv: by DOI if it matches their DOI pattern
        doi = payload.declared_doi or ""
        if BIORXIV_REGEX.match(doi):
            ident = f"doi:{doi}"
            url = f"https://api.biorxiv.org/details/biorxiv/{doi}"
            status, data = cached_get_json("biorxiv", "doi", ident, url)
            if status == 200 and data and data.get("collection"):
                coll = data["collection"]
                latest = coll[-1]
                hits.append(
                    PreprintHit(
                        server="biorxiv",
                        preprint_id=doi,
                        version_count=len(coll),
                        posted=latest.get("date") or "",
                        vor_doi=latest.get("published") or None,
                    )
                )
                return A08Output(hits=hits)

        # arXiv: search by title
        if any(
            tok in (bib.title or "").lower()
            for tok in ["arxiv", "preprint"]
        ) or "arxiv" in (bib.journal or "").lower():
            params = {
                "search_query": f"ti:{bib.title[:120]}",
                "max_results": 3,
            }
            ident = f"q:{hash(bib.title) & 0xffffffff:08x}"
            status, body, _ = cached_get(
                "arxiv",
                "search",
                ident,
                "http://export.arxiv.org/api/query",
                params=params,
            )
            if status == 200 and body:
                try:
                    root = ET.fromstring(body)
                    ns = {"a": "http://www.w3.org/2005/Atom"}
                    for entry in root.findall("a:entry", ns):
                        idtxt = (entry.findtext("a:id", default="", namespaces=ns) or "").strip()
                        # 'http://arxiv.org/abs/2401.12345v1'
                        m = re.search(r"abs/([^v\s]+)(v(\d+))?", idtxt)
                        if not m:
                            continue
                        arxiv_id = m.group(1)
                        version_count = int(m.group(3) or 1)
                        published = entry.findtext("a:published", default="", namespaces=ns) or ""
                        hits.append(
                            PreprintHit(
                                server="arxiv",
                                preprint_id=arxiv_id,
                                version_count=version_count,
                                posted=published,
                                vor_doi=None,
                            )
                        )
                        break
                except ET.ParseError:
                    pass

        return A08Output(hits=hits)

    def _verify(self, payload: IdentityQueryInput, output: A08Output) -> None:
        for hit in output.hits:
            if hit.server == "arxiv" and not ARXIV_REGEX.match(hit.preprint_id):
                self.fail(f"arxiv id {hit.preprint_id!r} fails regex")
            if hit.server in ("biorxiv", "medrxiv") and not BIORXIV_REGEX.match(
                hit.preprint_id
            ):
                self.fail(f"{hit.server} id {hit.preprint_id!r} fails regex")


# ---------- A09: Semantic Scholar ----------


class A09Output(_Frozen):
    s2_id: str | None
    doi: str | None
    title: str | None
    year: int | None
    citation_count: int | None
    influential_citation_count: int | None
    source: str = "semantic_scholar"


class SemanticScholarAgent(APIAgent[IdentityQueryInput, A09Output]):
    name = "A09_semantic_scholar"
    wave = "W2"

    BASE = "https://api.semanticscholar.org/graph/v1/paper"

    def _execute(self, payload: IdentityQueryInput) -> A09Output:
        fields = "paperId,externalIds,title,year,citationCount,influentialCitationCount"
        if payload.declared_doi and DOI_REGEX.match(payload.declared_doi):
            url = f"{self.BASE}/DOI:{payload.declared_doi}"
            ident = f"doi:{payload.declared_doi}"
            status, data = cached_get_json(
                "semantic_scholar", "doi", ident, url, params={"fields": fields}
            )
            if status == 200 and data:
                return _s2_to_output(data)

        bib = payload.bibliography
        query = bib.title
        params = {"query": query, "limit": 3, "fields": fields, "year": str(bib.year)}
        ident = f"q:{hash(query) & 0xffffffff:08x}:{bib.year}"
        status, data = cached_get_json(
            "semantic_scholar", "search", ident, f"{self.BASE}/search", params=params
        )
        if status != 200 or not data:
            return A09Output(
                s2_id=None,
                doi=None,
                title=None,
                year=None,
                citation_count=None,
                influential_citation_count=None,
            )
        for d in data.get("data") or []:
            if (d.get("year") or 0) == bib.year:
                return _s2_to_output(d)
        if data.get("data"):
            return _s2_to_output(data["data"][0])
        return A09Output(
            s2_id=None,
            doi=None,
            title=None,
            year=None,
            citation_count=None,
            influential_citation_count=None,
        )

    def _verify(self, payload: IdentityQueryInput, output: A09Output) -> None:
        if output.s2_id is not None and not re.match(r"^[0-9a-f]{40}$", output.s2_id):
            self.fail(f"S2 ID {output.s2_id!r} fails regex")


def _s2_to_output(d: dict) -> A09Output:
    ext = d.get("externalIds") or {}
    doi = (ext.get("DOI") or "").lower() or None
    return A09Output(
        s2_id=d.get("paperId"),
        doi=doi,
        title=d.get("title"),
        year=d.get("year"),
        citation_count=d.get("citationCount"),
        influential_citation_count=d.get("influentialCitationCount"),
    )


# ---------- A10: ORCID Resolution ----------


class AuthorORCID(_Frozen):
    position: int = Field(ge=1)
    name_in_bib: str
    orcid: str | None
    confidence: float = Field(ge=0.0, le=1.0)


class A10Output(_Frozen):
    authors: list[AuthorORCID]
    source: str = "orcid"


class ORCIDResolutionAgent(APIAgent[IdentityQueryInput, A10Output]):
    name = "A10_orcid_resolution"
    wave = "W2"

    BASE = "https://pub.orcid.org/v3.0/expanded-search"

    def _execute(self, payload: IdentityQueryInput) -> A10Output:
        out: list[AuthorORCID] = []
        # First try CrossRef ORCID extraction if DOI available — much higher precision.
        crossref_orcids: dict[int, str] = {}
        if payload.declared_doi and DOI_REGEX.match(payload.declared_doi):
            ident = f"doi:{payload.declared_doi}"
            status, data = cached_get_json(
                "crossref",
                "doi-orcid",
                ident,
                f"https://api.crossref.org/works/{urllib.parse.quote(payload.declared_doi, safe='')}",
            )
            if status == 200 and data:
                msg = data.get("message") or {}
                for i, a in enumerate(msg.get("author") or [], start=1):
                    orcid_url = a.get("ORCID") or ""
                    m = re.search(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", orcid_url)
                    if m and _orcid_checksum(m.group(1)):
                        crossref_orcids[i] = m.group(1)

        for a in payload.bibliography.authors:
            if a.position in crossref_orcids:
                out.append(
                    AuthorORCID(
                        position=a.position,
                        name_in_bib=a.name_verbatim,
                        orcid=crossref_orcids[a.position],
                        confidence=0.99,
                    )
                )
                continue
            # Fallback: ORCID Search by family + given
            given = (a.given or "").split(" ")[0] if a.given else ""
            params = {
                "q": f"family-name:{a.family} AND given-names:{given}" if given else f"family-name:{a.family}",
                "rows": 3,
            }
            ident = f"search:{a.family}:{given}"
            status, data = cached_get_json(
                "orcid",
                "search",
                ident,
                self.BASE,
                params=params,
                headers={"Accept": "application/json"},
            )
            orcid: str | None = None
            conf = 0.0
            if status == 200 and data:
                results = data.get("expanded-result") or []
                if len(results) == 1:
                    cand = results[0].get("orcid-id")
                    if cand and ORCID_REGEX.match(cand) and _orcid_checksum(cand):
                        orcid = cand
                        conf = 0.6
            out.append(
                AuthorORCID(
                    position=a.position,
                    name_in_bib=a.name_verbatim,
                    orcid=orcid,
                    confidence=conf,
                )
            )
        return A10Output(authors=out)

    def _verify(self, payload: IdentityQueryInput, output: A10Output) -> None:
        expected_n = len(payload.bibliography.authors)
        if len(output.authors) != expected_n:
            self.fail(
                f"author count mismatch: got {len(output.authors)}, expected {expected_n}"
            )
        for a in output.authors:
            if a.orcid is None:
                continue
            if not ORCID_REGEX.match(a.orcid):
                self.fail(f"ORCID {a.orcid!r} fails regex")
            if not _orcid_checksum(a.orcid):
                self.fail(f"ORCID {a.orcid!r} checksum fails")
