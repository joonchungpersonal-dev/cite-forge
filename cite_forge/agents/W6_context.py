"""W6 — Context & Semantic Capture.

A28 Citation Context Agent (LLM)
A29 Citation Function Classifier (LLM)
A30 MeSH/Topic Anchor Agent (API)
A31 Quote Verification Agent (Deterministic + LLM fallback)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cite_forge.agents.base import APIAgent, DeterministicAgent, LLMAgent
from cite_forge.schemas import CitationContext, CitationFunction, QuoteMatch


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------- A28: Citation Context ----------


class A28Input(_Frozen):
    unit_id: str
    ref_num: int
    manuscript_text: str
    claim_span: tuple[int, int]
    section_header: str
    paragraph_idx: int


class A28Output(_Frozen):
    sentence_verbatim: str
    sentence_span: tuple[int, int]
    prev_sentence: str | None = None
    next_sentence: str | None = None


class CitationContextAgent(DeterministicAgent[A28Input, A28Output]):
    """Pure-Python sentence extraction. The sentence containing the claim span
    is the smallest text region around claim_span bounded by sentence-ending
    punctuation. prev/next are the previous/next sentences in the same paragraph."""

    name = "A28_citation_context"
    wave = "W6"

    @staticmethod
    def _sentence_bounds(text: str, anchor: int) -> tuple[int, int]:
        # Walk back to start of sentence
        start = anchor
        while start > 0 and text[start - 1] not in ".!?\n":
            start -= 1
        while start < len(text) and text[start] in " \t\n":
            start += 1
        end = anchor
        # If anchor is at end of a sentence, scan forward to include its
        # terminating punctuation; otherwise scan to next sentence-end.
        while end < len(text) and text[end] not in ".!?\n":
            end += 1
        if end < len(text):
            end += 1
        return start, end

    def _execute(self, payload: A28Input) -> A28Output:
        text = payload.manuscript_text
        s, e = self._sentence_bounds(text, payload.claim_span[0])
        sentence = text[s:e]
        # Walk back to find previous sentence-end
        prev: str | None = None
        if s > 1:
            ps_end = s
            # find end of prev sentence
            k = s - 1
            while k > 0 and text[k] in " \t\n":
                k -= 1
            if k > 0:
                ps_start, ps_end2 = self._sentence_bounds(text, k)
                prev = text[ps_start:ps_end2]
        next_: str | None = None
        if e < len(text) - 1:
            ns_start = e
            while ns_start < len(text) and text[ns_start] in " \t\n":
                ns_start += 1
            if ns_start < len(text):
                _, ns_end = self._sentence_bounds(text, ns_start)
                next_ = text[ns_start:ns_end]
        return A28Output(
            sentence_verbatim=sentence,
            sentence_span=(s, e),
            prev_sentence=prev,
            next_sentence=next_,
        )

    def _verify(self, payload: A28Input, output: A28Output) -> None:
        text = payload.manuscript_text
        s, e = output.sentence_span
        if not (0 <= s < e <= len(text)):
            self.fail("sentence_span out of bounds")
        if text[s:e] != output.sentence_verbatim:
            self.fail("sentence_verbatim does not match manuscript at span")
        if output.prev_sentence and output.prev_sentence not in text:
            self.fail("prev_sentence not found in manuscript")
        if output.next_sentence and output.next_sentence not in text:
            self.fail("next_sentence not found in manuscript")


# ---------- A29: Citation Function Classifier ----------


class A29Input(_Frozen):
    unit_id: str
    ref_num: int
    citation_context: CitationContext


class A29Output(_Frozen):
    function: CitationFunction
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


_A29_SYSTEM = """You classify the rhetorical FUNCTION of an in-text citation.

CitationFunction values:
  BACKGROUND: cited for general background / framing of the field
  METHODS_BORROWED: cited because manuscript uses its method/instrument
  SUPPORTING_EVIDENCE: cited as direct empirical support for the claim
  CONTRADICTING: cited as evidence AGAINST the manuscript's claim
  CONTRAST_COMPARISON: cited to contrast with manuscript's design/result
  DEFINITIONAL: cited for a definition / canonical concept
  DATA_SOURCE: cited as the source of data used in the manuscript
  QUOTATION: a direct quote is taken from the cited work
  SELF_CITE: the citation is to the manuscript authors' own prior work

Pick the SINGLE best function. Confidence ∈ [0, 1]; if you cannot decide between two
functions with >0.7 each, return confidence < 0.7 so the system escalates."""


class CitationFunctionClassifier(LLMAgent[A29Input, A29Output]):
    name = "A29_citation_function"
    wave = "W6"
    model = "claude-haiku-4-5"

    def _execute(self, payload: A29Input) -> A29Output:
        from cite_forge.llm import call_structured

        ctx = payload.citation_context
        user = (
            f"Section: {ctx.section_header}\n"
            f"Previous: {ctx.prev_sentence or ''}\n"
            f"Citation sentence: {ctx.sentence_verbatim}\n"
            f"Next: {ctx.next_sentence or ''}\n"
        )
        result = call_structured(
            model=self.model,
            system=_A29_SYSTEM,
            user=user,
            output_cls=A29Output,
            tool_name="classify_function",
            tool_description="Emit the citation function and confidence.",
            max_tokens=1024,
        )
        return result.output

    def _verify(self, payload: A29Input, output: A29Output) -> None:
        if output.function not in CitationFunction:
            self.fail(f"function {output.function!r} not in enum")
        if output.confidence < 0.7:
            self.fail(
                f"function confidence {output.confidence:.2f} < 0.7 — must route to human queue"
            )


# ---------- A30: MeSH / Topic Anchor ----------


class A30Input(_Frozen):
    ref_num: int
    pmid: str | None
    openalex_id: str | None
    manuscript_topic_vector: list[float] | None  # optional pre-computed


class A30Output(_Frozen):
    mesh_terms: list[str]
    openalex_concepts: list[str]
    topic_vector: list[float]  # concatenated/embedded
    topic_drift_score: float | None = Field(default=None, ge=0.0, le=1.0)


class MeSHTopicAnchorAgent(APIAgent[A30Input, A30Output]):
    name = "A30_mesh_topic_anchor"
    wave = "W6"

    def _execute(self, payload: A30Input) -> A30Output:
        from cite_forge.http import cached_get, cached_get_json

        mesh: list[str] = []
        concepts: list[str] = []

        if payload.pmid:
            params = {"db": "pubmed", "id": payload.pmid, "retmode": "xml"}
            s, body, _ = cached_get(
                "pubmed",
                "efetch-mesh",
                payload.pmid,
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                params=params,
            )
            if s == 200 and body:
                import xml.etree.ElementTree as ET

                try:
                    root = ET.fromstring(body)
                    for desc in root.iter("DescriptorName"):
                        if desc.text:
                            mesh.append(desc.text)
                except ET.ParseError:
                    pass
        if payload.openalex_id:
            url = f"https://api.openalex.org/works/{payload.openalex_id}"
            s, data = cached_get_json(
                "openalex", "work-concepts", payload.openalex_id, url
            )
            if s == 200 and data:
                for c in data.get("concepts") or []:
                    name = c.get("display_name")
                    if name:
                        concepts.append(name)

        # Topic vector: token-presence over the union of MeSH + concepts.
        # Not an embedding — pure bag-of-tokens. Sufficient for A44 drift
        # cosine when both vectors share the same vocabulary axis.
        all_terms = sorted(set(mesh) | set(concepts))
        vec = [1.0 for _ in all_terms]
        if not vec:
            vec = [0.0]  # avoid empty
        return A30Output(
            mesh_terms=mesh,
            openalex_concepts=concepts,
            topic_vector=vec,
            topic_drift_score=None,
        )

    def _verify(self, payload: A30Input, output: A30Output) -> None:
        if payload.pmid is not None and not output.mesh_terms:
            self.fail("PMID provided but no MeSH terms returned")
        if not output.topic_vector:
            self.fail("topic_vector empty")


# ---------- A31: Quote Verification ----------


class A31Input(_Frozen):
    unit_id: str
    ref_num: int
    quote_in_manuscript: str
    pdf_full_text: str
    cited_page_in_manuscript: int | None  # if author specified page number


class A31Output(_Frozen):
    match: QuoteMatch
    distance: int | None  # Levenshtein distance for NEAR
    location_page: int | None
    location_offset: int | None


class QuoteVerificationAgent(DeterministicAgent[A31Input, A31Output]):
    """Honor Joon's feedback_veracity_quotes.md: paraphrased quotes are a FAIL.
    Verbatim match required for SUPPORTING_EVIDENCE quotations.
    """

    name = "A31_quote_verification"
    wave = "W6"

    NEAR_THRESHOLD = 0.95  # rapidfuzz partial_ratio normalized

    def _execute(self, payload: A31Input) -> A31Output:
        quote = payload.quote_in_manuscript.strip()
        text = payload.pdf_full_text
        idx = text.find(quote)
        if idx >= 0:
            return A31Output(
                match=QuoteMatch.VERBATIM,
                distance=0,
                location_page=None,
                location_offset=idx,
            )
        from rapidfuzz import fuzz

        score = fuzz.partial_ratio(quote, text) / 100.0
        if score >= self.NEAR_THRESHOLD:
            return A31Output(
                match=QuoteMatch.NEAR,
                distance=int((1.0 - score) * len(quote)),
                location_page=None,
                location_offset=None,
            )
        return A31Output(
            match=QuoteMatch.NOT_FOUND,
            distance=None,
            location_page=None,
            location_offset=None,
        )

    def _verify(self, payload: A31Input, output: A31Output) -> None:
        if output.match == QuoteMatch.VERBATIM:
            if output.location_offset is None:
                self.fail("VERBATIM without location_offset")
            text = payload.pdf_full_text
            quote = payload.quote_in_manuscript.strip()
            if text[output.location_offset : output.location_offset + len(quote)] != quote:
                self.fail("VERBATIM offset does not contain the quote")
