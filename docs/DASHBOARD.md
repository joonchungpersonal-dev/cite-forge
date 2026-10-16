# cite-forge Performance Dashboard — TRUE-Story Design

## Design thesis

Most audit dashboards show outcomes. This one shows **the work that produced the outcome**: every agent's verdict, every disagreement, every blocked path, every retry, every reach to an external API, every PDF rejected by the verifier, every gestalt finding. A citation that "passed" tells you nothing if you can't see what it was tested against.

The dashboard's job: make it impossible to look at a citation in the manifest and not know exactly how it got there.

## Architecture

```
~/.claude/audit-dashboard/index.html              # existing dashboard
    + new tab: "Citation Forge"

Reads from:
  ~/.claude/audit-trails/cite-forge/{run_id}/
      run_metadata.json
      manuscript_input.sha256
      units/U{NNNN}.json                          # atomic decomposition output
      citations/{NN:03d}/
          A01_atomic_decomposition.json
          A05_doi_resolution.json
          A06_pubmed.json
          ...
          A50_manifest_integrity.json
      convergence_critic.json                     # A33 verdict per citation
      cross_references.json
      escalations.json
      failures.json

  ~/.claude/cite-forge-log.json                   # run-level index (new)
```

Data is **inlined** via `<script id="cite-forge-data">` block per existing dashboard convention. No build step. Refresh dashboard → re-read trails → re-render.

## Tab structure (5 top-level views)

```
┌──────────────────────────────────────────────────────────────────┐
│ Code Review │ Veracity 555 │ Peer Review │ [Citation Forge] │ … │
└──────────────────────────────────────────────────────────────────┘

Within "Citation Forge":

┌────────────────┬────────────────────────────────────────────────┐
│ Manuscripts    │  [Selected Manuscript Detail]                  │
│                │                                                 │
│ ▸ TCM 2026     │  Tabs: Overview │ Citations │ Waves │ Gestalt │
│ ▸ Living Alone │         Sources │ Disagreements │ Ablation │   │
│ ▸ FLUX revision│         Replay                                  │
│ ▶ {selected}   │                                                 │
│                │  [view content]                                 │
└────────────────┴────────────────────────────────────────────────┘
```

---

## View 1 — Manuscript Overview

**Job:** global health snapshot. Answers "should I trust this manifest?"

```
╔═══════════════════════════════════════════════════════════════════╗
║ TCM Commentary (2026-04-27)             Run e2f4a1c1 │ 14 min ago ║
╠═══════════════════════════════════════════════════════════════════╣
║                                                                    ║
║  Citations                                                         ║
║  ┌────────────────────────────────────────────────────────────┐   ║
║  │ Total in bib:          59                                   │   ║
║  │ ┃ Converged → manifest: 54  ████████████████████░░  91.5%  │   ║
║  │ ┃ Escalated:             4  ██░░░░░░░░░░░░░░░░░░░░   6.8%  │   ║
║  │ ┃ Blocked (retracted):   1  ░░░░░░░░░░░░░░░░░░░░░░   1.7%  │   ║
║  └────────────────────────────────────────────────────────────┘   ║
║                                                                    ║
║  PDFs                                                              ║
║  ┌────────────────────────────────────────────────────────────┐   ║
║  │ Retrieved + verified:  54  ⬤ Europe PMC    24             │   ║
║  │ ILL-queued:             3  ⬤ Unpaywall     11             │   ║
║  │ Manual queue:           2  ⬤ Publisher OA  8              │   ║
║  │                            ⬤ PMC           6              │   ║
║  │                            ⬤ Preprint      3              │   ║
║  │                            ⬤ UM Proxy      2              │   ║
║  └────────────────────────────────────────────────────────────┘   ║
║                                                                    ║
║  Integrity flags                  Blind verification               ║
║  ┌──────────────────────┐         ┌──────────────────────────┐    ║
║  │ Retracted:        1  │         │ 3-of-3 agreed:    52     │    ║
║  │ Predatory risk:   0  │         │ 2-of-3 agreed:     2     │    ║
║  │ Preprint cited:   2  │         │ Disagreed:         0     │    ║
║  │ COI overlap:      0  │         │ A48 meta-rejected: 0     │    ║
║  │ Cartel flag:      0  │         └──────────────────────────┘    ║
║  └──────────────────────┘                                          ║
║                                                                    ║
║  Gestalt findings (new — W6.5)                                    ║
║  ┌────────────────────────────────────────────────────────────┐   ║
║  │ Paragraph verdicts:                                         │   ║
║  │ ┃ CALIBRATED   ████████████████████░░  37 / 42 paragraphs  │   ║
║  │ ┃ OVERCLAIM    ███░░░░░░░░░░░░░░░░░░░   3                  │   ║
║  │ ┃ CHERRYPICK   ██░░░░░░░░░░░░░░░░░░░░   2                  │   ║
║  │                                                              │   ║
║  │ Attribution inflation > 0.2:  3 (cites #14, #28, #43)       │   ║
║  │ Omitted caveats:              5 (across 4 cites)            │   ║
║  │ Causal language unjustified:  1 (cite #28)                  │   ║
║  │ Direction flips:              0  ✓                          │   ║
║  └────────────────────────────────────────────────────────────┘   ║
║                                                                    ║
║  Cost: $18.74    Agent calls: 4,213    Replay-deterministic: ✓    ║
╚═══════════════════════════════════════════════════════════════════╝
```

This page is the elevator pitch. Everything below is drill-down.

---

## View 2 — Per-Citation Forensic View (the heart)

**Job:** for ONE citation, show every agent that touched it.

```
╔═══════════════════════════════════════════════════════════════════╗
║ Citation [28]  Chung et al. 2023 (MESA mortality)         CONVERGED║
║ DOI: 10.1093/sleep/zsad048    PMCID: PMC10848217                  ║
║ Confidence: 0.94  (worst link: A53 = 0.94)                        ║
╠═══════════════════════════════════════════════════════════════════╣
║                                                                    ║
║  Agent timeline (left → right; vertical = wave)                   ║
║                                                                    ║
║  W0  ▓ A01 ✓ (claim spans 2,847-2,981; 1 cite anchor)             ║
║       ▓ A46 ✓ (section "Results" verified)                        ║
║                                                                    ║
║  W1  ▓ A02 ✓  ▓ A03 ✓  ▓ A04 ✓                                    ║
║                                                                    ║
║  W2  ▓ A05 → DOI 10.1093/sleep/zsad048 (CrossRef, score 1402.5)   ║
║       ▓ A06 → PMID 37000000  PMCID PMC10848217  MeSH: 8 terms     ║
║       ▓ A07 → OpenAlex W4378219412  ⬤ unpaywall  ⬤ europepmc       ║
║       ▓ A08 → no preprint                                          ║
║       ▓ A09 → S2 a3f...e8  cite count 47  influential 12          ║
║       ▓ A10 → 7 authors, 5 ORCIDs resolved (2 unresolved)         ║
║                                                                    ║
║  W3  ▓ A11 ✓ CONVERGED (0 dissenting)                             ║
║       ░ A12 — skipped (single candidate)                          ║
║       ▓ A13 ✓ all fields match (CrossRef ↔ PubMed ↔ OpenAlex)     ║
║       ▓ A40 ✓ author positions match across sources               ║
║       ▓ A41 ✓ dates within 11 days                                ║
║       ▓ A43 ✓ DOI round-trip OK                                   ║
║                                                                    ║
║  W4  ▓ A14 ✓ NONE     ▓ A15 ✓ risk 8    ▓ A16 ✓ no preprint       ║
║       ▓ A17 ✓ no COI  ▓ A18 — bib-level ▓ A45 ✓ no Crossmark      ║
║                                                                    ║
║  W5  ▓ A19 → chose europepmc (published, 5382KB)                  ║
║       ▓ A20–A25 source probes:                                    ║
║          A20 unpaywall      ✓ 2129KB   100ms                       ║
║          A21 europepmc      ✓ 5382KB   240ms   ← chosen           ║
║          A22 pmc            ✓ 5382KB   330ms                       ║
║          A23 preprint       — no hit                              ║
║          A24 publisher_oa   — paywalled                           ║
║          A25 um_proxy       — not attempted (OA satisfied)         ║
║       ▓ A27 ✓ title cosine 0.97  author ✓  DOI imprint ✓  year ✓  ║
║       ▓ A38 ✓ page-2 abstract keywords match 7/10                 ║
║                                                                    ║
║  W6  ▓ A28 ✓  ▓ A29 ✓ SUPPORTING_EVIDENCE (conf 0.92)             ║
║       ▓ A30 ✓ MeSH: Sleep, MESA, Mortality, Cohort Studies        ║
║       ▓ A31 — no quote   ▓ A47 — skipped                          ║
║                                                                    ║
║ ⚠W6.5 ▓ A51 ⚠ OVERCLAIM   evidence "definitively shows"           ║
║              found in manuscript; source uses "associated with"   ║
║       ▓ A52 ✓ 0 connectives flagged                                ║
║       ▓ A53 ⚠ inflation 0.18    STRENGTHENED_VERB                  ║
║              "associated with" (source) → "drives" (manuscript)   ║
║       ▓ A54 ✓ no caveats omitted                                   ║
║       ▓ A55 ⚠ causal "drives" not justified by cohort design     ║
║       ▓ A56 ✓ all 3 numbers verified verbatim                     ║
║       ▓ A57 ✓ narrative sound                                      ║
║       ▓ A58 ⚠ hedge delta +2 (source: weak, manuscript: strong)    ║
║       ▓ A39 ✓ PDF SUPPORTS claim (passage on p.6)                 ║
║       ▓ A44 ✓ topic drift 0.08                                     ║
║                                                                    ║
║  W7  ▓ A32 (blind, Opus)  → DOI 10.1093/sleep/zsad048  ✓ matches  ║
║       ▓ A36 (blind, Haiku) → DOI 10.1093/sleep/zsad048  ✓ matches  ║
║       ▓ A33 ✓ CONVERGED (all 17 clauses)                          ║
║       ▓ A37 ✓ triangulated (3/3)                                  ║
║       ▓ A42 ✓ retraction recheck NONE                              ║
║       ▓ A48 ✓ SOUND consensus                                      ║
║                                                                    ║
║  W8  ▓ A34 ✓ admitted to manifest                                  ║
║       ▓ A50 (manuscript-level) — sampled, passed                  ║
║                                                                    ║
║  Confidence breakdown (worst-link rule)                            ║
║  ┌──────────────────────────────────────────────────────────┐     ║
║  │ A53 attribution_inflation_score  ████░░░░░░░░  0.94 *MIN* │     ║
║  │ A29 function_confidence          ██████████░░  0.92       │     ║
║  │ A11 identity_convergence         ████████████  1.00       │     ║
║  │ A27 pdf_verification             ████████████  0.97       │     ║
║  │ ...                                                       │     ║
║  └──────────────────────────────────────────────────────────┘     ║
╚═══════════════════════════════════════════════════════════════════╝
```

**Why this matters:** the convergence rule returned True, but the dashboard shows that A53 inflation was close to the threshold (0.18 with 0.20 limit) AND A51 reported OVERCLAIM AND A55 flagged unjustified causal language. The user can see *the citation passed barely* and decide whether to revise the manuscript even though the pipeline didn't block.

---

## View 3 — Wave Performance Heatmap

**Job:** identify systematic weaknesses across the manuscript.

```
Wave \ Cite  1  2  3  4  5  6  7  8  9 10 ... 59
─────────────────────────────────────────────────
W0  A01     ✓  ✓  ✓  ✓  ✓  ✓  ✓  ✓  ✓  ✓  ...  ✓
W1  A02     ✓  ✓  ✓  ✓  ✓  ✓  ✓  ✓  ✓  ✓
W2  A05     ✓  ✓  ⨯  ✓  ✓  ✓  ⚠  ✓  ✓  ✓
    A06     ✓  ✓  ✓  ✓  ✓  ✓  ⨯  ✓  ✓  ✓
    A07     ✓  ✓  ✓  ✓  ✓  ✓  ✓  ✓  ✓  ✓
    A08     ✓  ─  ─  ─  ✓  ─  ─  ─  ✓  ─
W3  A11     ✓  ✓  ⚠  ✓  ✓  ✓  ⨯  ✓  ✓  ✓
W4  A14     ✓  ✓  ✓  ✓  ✓  ✓  ✓  ✓  ⨯  ✓  ← cite 9 RETRACTED
W5  A27     ✓  ✓  ✓  ⨯  ✓  ✓  ✓  ✓  ─  ✓
W6  A29     ✓  ✓  ✓  ─  ✓  ⚠  ✓  ✓  ─  ✓
W6.5 A53    ✓  ✓  ✓  ─  ✓  ⨯  ⨯  ✓  ─  ⚠ ← attribution inflation cluster
W7  A33     ✓  ✓  ⨯  ⨯  ✓  ⨯  ⨯  ✓  ⨯  ✓
W8  →manifest? ✓  ✓  ─  ─  ✓  ─  ─  ✓  ─  ✓

Legend: ✓ pass  ⚠ low conf  ⨯ fail  ─ skipped/not applicable
```

Vertical clusters of `⨯` reveal a systematic gap. Horizontal clusters reveal which agents are routinely binding. **This is the view that drives ablation hypotheses.**

---

## View 4 — Identity Triangle Diff (W3 vs Blind1 vs Blind2)

**Job:** the most accuracy-load-bearing comparison in the system.

```
Citation [37]  De Francesco 2021 HIV

         W3 consensus           Blind 1 (Opus)         Blind 2 (Haiku)
         (Sonnet pipeline)
DOI:     10.1093/sleep/zsab085  10.1093/sleep/zsab085  10.1093/sleep/zsab085  ✓✓
PMID:    33864380               33864380               33864380               ✓✓
PMCID:   PMC8408351             PMC8408351             null                   ⚠
OAlex:   W3162348917            W3162348917            W3162348917            ✓✓

Convergence: A33 → CONVERGED  (PMCID null on Haiku is non-blocking)
Triangulation A37: 2/3 pairs fully agree, 1/3 partial.

⚠ Inspect Haiku's call:
   prompt: see audit-trails/cite-forge/.../citations/037/A36_blind_rederivation_2.json
   response: "No PMCID found via metadata lookup; relying on PMID."
   verdict: A48 meta-critic: SOUND (Haiku's omission, not error)
```

---

## View 5 — Source Provenance Map

**Job:** show exactly how each PDF was acquired. Per-source success rate. Latency. Bytes.

```
Source              Hit  Miss  Hit %   Avg ms   Avg KB     Chosen
─────────────────────────────────────────────────────────────────
A20 Unpaywall        47    12   80%      180     1,847       11
A21 Europe PMC       45    14   76%      290     3,422       24
A22 PMC              31    28   53%      410     3,219        6
A23 arXiv/preprint    8    51   14%      120     2,103        3
A24 Publisher OA     12    47   20%      850       N/A        8
A25 UM Proxy         18    41   31%    2,400     2,891        2
A26 ILL Fallback      3   N/A    —    queue       N/A        0
─────────────────────────────────────────────────────────────────
Citations: 54 / 59 with verified PDF; 3 in ILL; 2 in manual queue.

Bytes downloaded: 167 MB
Wall-clock retrieval: 6m 14s (parallel across sources × citations)
```

Per-citation detail on hover: HTTP status, response headers, License field, retrieval timestamp, sha256 hash, who chose it (A19 rationale).

---

## View 6 — Gestalt Findings (W6.5)

**Job:** surface the rhetorical-level failures that single-claim audits miss. **This is new with cite-forge.**

```
Paragraph 14 (Discussion, p.7)            VERDICT: OVERCLAIM
"Sleep regularity definitively shows superior predictive power 
 over duration, driving cardiovascular mortality outcomes 
 across populations."

Cites: [27] Windred 2024, [12] Chung 2023

Findings:
  A51 OVERCLAIM
       Source language: "Sleep regularity emerged as a stronger 
                         predictor of mortality risk than sleep duration"
                         (Windred 2024, abstract)
       Manuscript: "definitively shows superior predictive power...
                    driving cardiovascular mortality"
       
  A53 inflation 0.34  (over threshold 0.20)
       Mechanism: STRENGTHENED_VERB + ASSOCIATION_TO_CAUSATION
       "predictor" → "drives"
  
  A55 causal "driving" not justified
       Source design: LONGITUDINAL_COHORT (UK Biobank)
       Cohort does not support causal verb without IV/MR design.
  
  A58 hedging delta +3  (uncalibrated)
       Manuscript hedge strength: 5 ("definitively shows")
       Source hedge strength:     2 ("emerged as a stronger predictor")
  
  A57 narrative SOUND (inference itself is coherent)

Recommendation: revise sentence to track source language.
Suggested rewrite (LLM-drafted):
  "Sleep regularity has emerged as a stronger predictor of 
   cardiovascular mortality than sleep duration in population cohorts."
```

This view answers "where did the manuscript get rhetorically too big for its evidence."

---

## View 7 — Disagreement Map

**Job:** show the six W2 identity agents on every citation as a small-multiples grid.

```
Citation [22]  Hoepel 2025 LCA

  Source           DOI                          Confidence
  A05 CrossRef     10.1016/j.sleh.2025.01.004   1.00
  A06 PubMed       (no PMID — not yet indexed)  —
  A07 OpenAlex     10.1016/j.sleh.2025.01.004   1.00
  A08 Preprint     — no preprint
  A09 Semantic S.  10.1016/j.sleh.2025.01.004   0.97
  A10 ORCID        5/6 authors resolved         —

Convergence A11: CONVERGED (PMID null is permissive; 3 non-null DOIs agree)

⚠ Inspect: PubMed not indexed yet. Watch for indexing delay; A42 will recheck.
```

Filter: show only citations with ≥1 disagreement. Sort: by severity.

---

## View 8 — Failure Timeline

**Job:** for failed citations, chronicle where things went wrong.

```
Citation [4]  Hale 2020 Sleep Health Equity

  14:02:11  A01 → unit U0008 ✓
  14:02:14  A02 → surface "(Hale et al., 2020)" ✓
  14:02:17  A05 CrossRef → 3 candidates (3 papers w/ first author Hale, 2020)
  14:02:18  A12 disambiguation → conf 0.81  (< 0.85 threshold)
  14:02:18  A12 ESCALATED to human queue
  14:02:18  pipeline emits packet → ESCALATIONS.md#cite-4
  
  Question for human:
  "CrossRef returned 3 candidates. The manuscript paragraph discusses
   sleep health equity in public health agendas. Two candidates
   match the topic; one is an unrelated 2020 Hale paper on chronic
   pain. Confidence in candidate 0 (DOI 10.1146/annurev-publhealth-…)
   is 0.81. Should we promote to manifest, or are we missing context?"
```

---

## View 9 — Ablation Explorer (interactive)

**Job:** the user clicks an agent to drop it; convergence is re-evaluated from cache; deltas are shown immediately. **Zero API cost.**

```
Drop agent: [A53 attribution_inflation ▼]   [Re-evaluate]

Result with A53 dropped:
  Citations converged:    54 → 57  (+3)
  Citations escalated:     4 → 1  (-3)
  Citations blocked:       1 → 1  ( 0)

Newly-converged citations (formerly escalated):
  ┃ [14] Bertisch 2023 — inflation 0.24 was binding
  ┃ [28] Chung 2023    — inflation 0.18 was binding (now noise-only)
  ┃ [43] Billings 2020 — inflation 0.22 was binding

Conclusion: A53 is meaningful — it catches inflation that the gestalt
agent (A51) would otherwise let through. Recommend keep.
```

Pair drop: `[A53] + [A55]`. Triple drop. Multi-agent comparison overlay.

---

## View 10 — Cost & Latency Accounting

```
Per-wave wall-clock & cost                Per-agent total cost
─────────────────────────────             ─────────────────────────────
W0     8s    $0.12                        A50 manifest int.   $2.14
W1    24s    $0.41                        A33 convergence     $0.00  (det.)
W2  3m 12s   $1.84  ← network-heavy       A53 inflation       $1.91
W3    47s    $0.74                        A51 gestalt         $1.78
W4  1m 30s   $0.92                        A28 context         $1.42
W5  6m 14s   $3.21  ← retrieval-heavy     A57 narrative       $1.31
W6  1m 22s   $1.65                        A56 numeric         $1.08
W6.5  2m 4s  $4.81  ← gestalt cluster     A01 decomposition   $0.95
W7  1m 51s   $3.47                        A29 function        $0.81
W8    19s    $1.57                        A35 escalation      $0.78
─────────────────                         ...
Total: 18m  $18.74                        43 LLM agents, 15 deterministic

Cache hit rate (on replay): 100%   Determinism check: ✓ identical hash
```

---

## View 11 — Replay Tester

```
Run e2f4a1c1  (original)        |  Replay run 8d2bbc91  (cache-backed)
manifest sha256: a3f8…b2c1      |  manifest sha256: a3f8…b2c1      ✓ MATCH

Agent call hashes:
  A01_atomic_decomposition.json  matches  ✓
  A05_doi_resolution.json        matches  ✓
  ...
  All 4,213 calls reproduce byte-identically.
```

If a replay drifts, this view is where the drift surfaces immediately. It's the regression line for the whole system.

---

## Cross-cutting elements

### Color discipline
- **Green** (`#0ea674`) = clause passed
- **Amber** (`#d6932a`) = passed but close to threshold OR not applicable
- **Red** (`#c0392b`) = clause failed; manifest exclusion
- **Slate** (`#64748b`) = skipped / N/A
- **Indigo** (`#4f46e5`) = blind re-derivation
- **Magenta** (`#c026d3`) = gestalt / W6.5 finding (intentionally distinct — these are the new accuracy frontier)

### Anti-falsification design
The dashboard reads from **immutable, append-only trail files**. There's no separate "summary" data file that could drift from the underlying calls. If the dashboard shows "A27 passed for citation 12," that is because `citations/012/A27_pdf_verification.json` has `verifier_passed: true`. The trail is the only source of truth. The dashboard renders it; it does not summarize it.

### Hover-to-drill-down
Every cell in the heatmap, every dot on the timeline, every confidence bar — hovering reveals the raw call record: prompt hash, model, response excerpt, verifier reason, latency, cost. **Nothing is hidden.**

### Diff-against-prior-run
For manuscripts that have been run multiple times (e.g., before and after revision), every view supports diff mode: show what changed citation-by-citation, agent-by-agent.

---

## Data contract

The dashboard requires the following per run:

```json
{
  "run_id": "...",
  "manuscript": { "path": "...", "sha256": "..." },
  "metadata": { ... },
  "citations": [
    {
      "ref_num": 28,
      "convergence_verdict": "CONVERGED",
      "overall_confidence": 0.94,
      "binding_agent": "A53_attribution_inflation",
      "agent_outputs": {
        "A01_atomic_decomposition": { "verifier_passed": true, "output": {...} },
        ...
      },
      "trail_dir": "audit-trails/cite-forge/{run_id}/citations/028/"
    }
  ],
  "units": [ ... ],
  "escalations": [ ... ],
  "failures": [ ... ],
  "wave_stats": { "W0": {...}, ... },
  "source_stats": { "A20_unpaywall": {...}, ... },
  "cost": { "total_usd": 18.74, "per_agent": {...} }
}
```

The Python package writes this on every run via `cite_forge.dashboard.export_for_dashboard(run_dir)` — a small module to add later.

---

## What this dashboard refuses to do

- **Show a smoothed/averaged "system health" score.** Citations don't average — they pass or fail per the convergence rule. Hiding behind a 92.4% score would be exactly the lying-with-statistics this system is designed to prevent.
- **Auto-fix anything.** The dashboard surfaces; the user (or `/cite-forge` in next run) acts.
- **Provide an "ignore this finding" button.** Escalation packets exit the trail. They don't get cleared from the dashboard.
- **Cache its own derived state.** Every view re-reads the trail.

---

## Build path

| Phase | Scope | Effort |
|-------|-------|--------|
| 1 | Static HTML stub at `~/cite-forge/docs/dashboard-mockup.html` with synthetic data — proves the layout | 1 day |
| 2 | `cite_forge.dashboard.export_for_dashboard()` writes consolidated JSON for the dashboard | 1 day |
| 3 | Citation Forge tab added to `~/.claude/audit-dashboard/index.html` (View 1 + View 2 only) | 2 days |
| 4 | Views 3–7 | 3 days |
| 5 | Views 8–11 + interactivity (ablation explorer) | 2 days |
| 6 | Diff-against-prior-run mode | 1 day |

Build phase 1 immediately on first real cite-forge run so the data shape is exercised end-to-end before investing in the visual.
