# cite-forge — Agent Specification (frozen)

**Version:** 1.0.0
**Standard:** Absolute accuracy. Efficiency is not the metric.
**Principle:** Hard-code invariants; delegate judgment; verify every LLM output deterministically; cross-check every judgment against an independent agent.

58 agents across 10 waves (W0 through W8 plus W6.5 Gestalt). Each agent has: input contract, task, output schema, deterministic verifier, escalation rule, trail file.

---

## Wave structure

| Wave  | Purpose | Registered agent count |
|-------|---------|------------------------|
| W0    | Manuscript decomposition | 2 |
| W1    | Citation discovery | 3 |
| W2    | Identity resolution | 6 |
| W3    | Reconciliation | 6 |
| W4    | Integrity checks | 6 |
| W5    | Retrieval | 10 |
| W6    | Context & semantic | 5 |
| W6.5  | **Gestalt** (semantic + paragraph-level) | 10 |
| W7    | Blind verification | 6 |
| W8    | Manifest assembly | 4 |
| **Total** | | **58** |

Distributed verifiers (A38-A50, A40, A41, A43, A45, A46, A47, A49, A50, A39, A44, A48, A42) live in `verification.py` but register into the wave whose work they cross-check.

---

## W0 — Decomposition

### A01 Atomic Decomposition Agent (LLM)
Splits manuscript into atomic claim-citation units. One unit per declarative claim. Compound claims forbidden.
Verifier: `manuscript_text[span] == claim_verbatim`; every citation_anchor within ±200 chars of claim_span.

### A46 Section-Boundary Verifier (Deterministic)
Confirms assigned section_header appears upstream of claim_span in the manuscript text.

---

## W1 — Discovery (parallel)

### A02 Citation Extraction (LLM)
Pulls every in-text citation surface form with span, normalized author + year.
Verifier: `text[span] == surface`; year ∈ [1500, current_year+1].

### A03 Bibliography Parsing (LLM)
Parses bibliography into structured records.
Verifier: DOI regex on declared DOIs; minimum-viable fields.

### A04 Cross-Reference Audit (Deterministic)
Pairs in-text citations to bibliography entries; expands range citations; flags orphans both directions.

---

## W2 — Identity Resolution (parallel per citation)

Six independent agents query independent authorities. Do **not** see each other's outputs.

| ID | Agent | Authority |
|----|-------|-----------|
| A05 | DOI Resolution | CrossRef REST |
| A06 | PubMed | NCBI E-utilities (PMID, PMCID, MeSH, retraction linkages) |
| A07 | OpenAlex | Unified ID graph + OA-locations + host-type tags |
| A08 | Preprint Servers | arXiv / bioRxiv / medRxiv / SSRN / OSF / ResearchSquare |
| A09 | Semantic Scholar | Metadata + influential-citation indicator |
| A10 | ORCID Resolution | Every author position (not just first) |

---

## W3 — Reconciliation

### A11 Identity Convergence Critic (Deterministic)
Computes consensus across W2 outputs. Hard-coded rule: all non-null IDs must agree, ≥1 must be present.

### A12 Disambiguation (LLM)
Returns candidate **index** (Literal, not free-form DOI). Confidence < 0.85 → human queue.

### A13 Bibliographic Field Reconciliation (LLM + Deterministic)
Per-field consensus across CrossRef/PubMed/OpenAlex. Discrepancies require ≥2-source backing.

### A40 Multi-Position Author Verifier (Deterministic)
Every author position matches across CrossRef/PubMed/OpenAlex. Catches author-order swaps and ghost coauthors.

### A41 Publication Date Triangulator (Deterministic)
CrossRef + PubMed + OpenAlex pub dates agree within 30 days.

### A43 DOI Round-Trip Verifier (API)
DOI → metadata → independent CrossRef call → matches original.

---

## W4 — Integrity (parallel per citation)

### A14 Retraction & Erratum (API) — **refuse-to-gold-standard**
Retraction Watch + PubMed retraction linkages + CrossRef relations. Refuses to mark RETRACTED as gold-standard.

### A15 Predatory Venue (API)
DOAJ / OASPA / archived Beall's. Risk score 0-100.

### A16 Preprint vs Version-of-Record (API + LLM)
Computes delta when preprint cited but VOR exists.

### A17 Funding & COI (API)
CrossRef funder registry + full text. Flags funder overlap with manuscript authors.

### A18 Citation Cartel Detector (Deterministic)
Bib-level concentration stats. >25% from one author/journal triggers flag.

### A45 Crossmark Verifier (API)
Checks for corrections/withdrawals/updates published after the paper.

---

## W5 — Retrieval (parallel per citation)

### A19 Retrieval Coordinator (Deterministic preference rules)
Probes A20-A25 in parallel; prefers published over preprint; prefers publisher OA over repository.

### A20-A26 Source Specialists
- A20 Unpaywall
- A21 Europe PMC
- A22 PubMed Central
- A23 arXiv / bioRxiv / medRxiv
- A24 Publisher OA (PLOS, Frontiers, MDPI, Nature OA, eLife, Wiley OA, Elsevier OA)
- A25 UM Library Proxy (Shibboleth/SAML)
- A26 ILL Fallback (DOCLINE/ILLiad; non-blocking)

### A27 PDF Verification Agent (LLM + Deterministic) — **THE PRIMARY ACCURACY GATE**
Opens PDF; extracts title page, abstract, first-author surname, DOI imprint. Compares to expected metadata.
FAILS even on HTTP 200 if title cosine < 0.90, first-author mismatch, year mismatch, or DOI imprint mismatch.

### A38 Page-2 PDF Verifier (LLM)
Beyond title page: methods/intro content match. Catches journal-homepage-served-as-PDF.

---

## W6 — Context & Semantic Capture

### A28 Citation Context Agent (LLM)
Per W0 unit: sentence verbatim + prev + next + section header.

### A29 Citation Function Classifier (LLM)
Classifies as `BACKGROUND` / `METHODS_BORROWED` / `SUPPORTING_EVIDENCE` / `CONTRADICTING` / `CONTRAST_COMPARISON` / `DEFINITIONAL` / `DATA_SOURCE` / `QUOTATION` / `SELF_CITE`.

### A30 MeSH/Topic Anchor Agent (API)
PubMed MeSH + OpenAlex concepts → topic-anchor vector.

### A31 Quote Verification Agent (Deterministic + LLM fallback)
For quoted material: VERBATIM_MATCH required. Paraphrased quotes are a FAIL.

### A47 Quote Pagination Verifier (Deterministic)
When manuscript cites a page number, verify quote appears on that page in PDF.

---

## W6.5 — Gestalt / Semantic Committee (NEW)

Operates over **multi-sentence units**. A sentence can be individually citation-valid yet leave the reader with a wrong impression. These agents close that gap.

### A51 Paragraph Gestalt Agent (LLM)
Reads paragraph + all cited PDFs. Verdict: `CALIBRATED` / `OVERCLAIM` / `UNDERCLAIM` / `CHERRYPICK` / `FALSE_CONSENSUS`.
Verifier: non-CALIBRATED verdicts require evidence sentences (verbatim from paragraph) AND source passages (verbatim from cited PDFs).

### A52 Sentence-Pair Logical Connective Agent (LLM)
For connective-joined adjacent sentences ("therefore", "however", "thus"): does the connective match the logical relationship?
Relations: `CAUSAL` / `CORRELATIONAL` / `CONTRASTIVE` / `ADDITIVE` / `CONCESSIVE` / `TEMPORAL` / `EVIDENTIAL`.

### A53 Attribution Inflation Detector (LLM)
Is the manuscript's claim stronger than the source supports?
Mechanisms: `STRENGTHENED_VERB` / `DROPPED_QUALIFIER` / `GENERALIZED_POPULATION` / `TEMPORAL_SHIFT` / `SAMPLE_TO_POPULATION` / `ASSOCIATION_TO_CAUSATION`.
Verifier: source_language AND manuscript_language must be verbatim from their respective texts.

### A54 Omitted-Caveat Detector (LLM)
Identifies caveats/limitations/counter-evidence in source omitted by manuscript.
Verifier: every caveat_verbatim is exact substring of source PDF text.

### A55 Causal Language Audit (LLM + Deterministic)
Causal language ("causes", "drives", "leads to") justified only by RCT / quasi-experiment / Mendelian randomization / instrumental variable.
Cross-sectional, longitudinal cohort, case-control: NOT causally justified.

### A56 Numeric Claim Verifier (LLM)
For every number cited: verify exact number in source AND direction of effect matches.
`DIRECTION_FLIP` is a **manifest blocker**.

### A57 Narrative Coherence Agent (LLM)
Does the inference chain (premise → premise → conclusion) flow through the cited evidence?
Verdict: `SOUND` / `BROKEN` / `UNVERIFIABLE`.

### A58 Hedging Calibration Agent (LLM)
Manuscript hedge strength (1=weakest, 5=strongest) vs. source hedge strength. |Δ| > 1 = uncalibrated.

### A39 Claim-Source Support Verifier (LLM)
Per claim: does PDF contain evidence? `SUPPORTS` / `WEAK_SUPPORT` / `NOT_FOUND` / `CONTRADICTS`. Sanity gate before downstream veracity audit.

### A44 Topic Drift Detector (Deterministic + embedding)
Cosine between source's topic vector and manuscript's topic vector. Catches right-paper-wrong-topic miscites.

---

## W7 — Blind Verification

### A32 Blind Re-Derivation #1 (LLM, Opus 4.7 — different model from production Sonnet)
Independently re-runs W0–W3 with no access to prior outputs.

### A36 Blind Re-Derivation #2 (LLM, Haiku 4.5 — third independent model)
Second independent re-run. 3-of-3 convergence rule requires both blinds to agree with W3.

### A33 Convergence Critic (Deterministic — NOT LLM)
The master convergence rule. Pure Python `and` of every clause; see `pipeline.is_converged()`.

### A37 Triangulation Critic (Deterministic)
3-of-3 identity convergence: w3 ↔ blind1 ↔ blind2 all pairwise agree.

### A42 Retraction Recheck Critic (API)
Independent re-query at end-of-run. **Fails closed.**

### A48 Identity-Consensus Meta-Critic (LLM)
Catches false-consensus: all 6 W2 agents share the same upstream metadata error.

---

## W8 — Manifest Assembly

### A34 Manifest Schema Validator (Deterministic — NOT LLM)
Refuses to emit on:
- SCHEMA_VERSION mismatch
- RETRACTED status without `--allow-retracted`
- `overall_confidence < 0.85`
- `convergence_verdict != CONVERGED`
- Round-trip parse failure

### A35 Human Escalation Packetizer (LLM)
Renders ESCALATIONS.md with required headings: Context / Conflicting evidence / Question / Candidates.

### A49 Escalation Packet Validator (Deterministic)
Verifies every packet has required headings, specific question, candidate PDFs.

### A50 Manifest Integrity Auditor (LLM + Deterministic)
Random-samples 10% of manifest; re-verifies. >1% failure rate → manifest reissue.

---

## Convergence rule (hard-coded in `pipeline.is_converged()`)

```python
converged = all([
    _ids_triangulate(w3_consensus, blind1),
    _ids_triangulate(w3_consensus, blind2),
    _ids_triangulate(blind1, blind2),
    retraction_known,
    retraction_recheck_agrees,
    pdf_verified,
    pdf_verified_page2,
    context_captured,
    function_confidence >= 0.7,
    not quote_required or quote_verbatim_matched,
    gestalt_calibrated,
    attribution_inflation_score <= 0.2,
    causal_language_justified,
    all_numeric_claims_verified,
    no_direction_flips,
    narrative_sound,
    integrity_audit_passed,
])
```

No agent votes convergence on its own. The rule is reviewable, diff-able, replayable.

---

## Confidence + escalation thresholds

| `overall_confidence` | Action |
|---|---|
| ≥ 0.95 | Auto-accept |
| 0.85 – 0.95 | Included in manifest, flagged for spot-check |
| < 0.85 | NOT in manifest; routed to `ESCALATIONS.md` |

`overall_confidence = min(across all relevant agents)` (worst-link rule).

---

## Trail layout

```
~/.claude/audit-trails/cite-forge/{run_id}/
    run_metadata.json
    manuscript_input.sha256
    cross_references.json
    convergence_critic.json
    escalations.json
    failures.json
    units/
        U0001.json
    citations/
        001/
            A01_atomic_decomposition.json
            A05_doi_resolution.json
            A06_pubmed.json
            ...
            A58_hedging_calibration.json
```

Each agent's file: `{agent, wave, calls: [AgentCallRecord, ...]}`. Multi-call agents append cleanly. `cite-forge replay <run_id>` re-executes deterministically.

---

## Ablation (`cite_forge.ablation`)

Cache-backed. Cost: **~$0** after the initial run.

- `leave_one_out(run_dir)` — re-evaluates convergence per citation with each agent dropped
- `pair_ablation(run_dir)` — every pair; C(n,2) results
- `binding_fraction(run_dir)` — fraction of citations where an agent was load-bearing

The convergence rule reads directly from 16 of 58 agents; the other 42 feed those 16. LOO on the 16 reveals verdict-level dependencies; LOO on the others reveals upstream contributions.
