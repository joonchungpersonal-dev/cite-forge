# cite-forge

Accuracy-first citation framework for academic manuscripts.

Given a draft (Markdown, .docx, or PDF), `cite-forge`:

1. Extracts every in-text citation and bibliography entry
2. Resolves each to a unified identifier graph (DOI / PMID / PMCID / arXiv / OpenAlex)
3. Verifies the identity via independent agents until convergence
4. Retrieves the open-access PDF from a multi-source cascade
5. Validates that the PDF matches the cited work
6. Captures citation context, function, and quote integrity
7. Emits a frozen-schema manifest consumed by downstream veracity audits

**Design principle.** Hard-code invariants. Delegate judgment. Verify every LLM output deterministically. Cross-check every judgment against an independent agent.

**Accuracy is the north star. Efficiency is not the standard.**

## Quick start

```bash
pip install -e .
cite-forge build path/to/manuscript.md --slug tcm-commentary
```

Output locations are derived from where the draft sits. For a draft at
`<root>/drafts/v3.md`, the run writes into `<root>/`:

- `papers/{NN}_{Author}_{Year}_{Topic}.pdf`, the retrieved corpus
- `manifest.json`, the frozen-schema audit manifest
- `bibliography.{md,bib,ris,json}`, exports for reference managers
- `MISSING.md`, the list of citations whose PDF could not be retrieved, with
  per-citation retrieval instructions
- `ESCALATIONS.md`, human-review packets for citations that did not converge

The full agent trail for the run, including every agent's input, output and
verifier verdict, plus `failures.json`, goes to
`~/.claude/audit-trails/cite-forge/{run_id}/`.

## Architecture

See [`AGENTS.md`](./AGENTS.md) for the full spec: 58 agents across 10 waves
(W0 through W8, plus W6.5 for paragraph-level gestalt checks). Each agent
declares an input contract, an output schema, a deterministic verifier, an
escalation rule and a trail file.

Convergence is not a model judgment. `cite_forge.pipeline.is_converged()` is a
pure-Python conjunction of 17 clauses: the W3 consensus identifiers and both
blind re-derivations must triangulate pairwise, retraction status must be known
and must survive a recheck, the PDF must verify on the title page and on page 2,
citation context must be captured, citation-function confidence must be at least
0.7, any required quote must match verbatim, the paragraph gestalt must be
calibrated, the attribution-inflation score must be at most 0.2, causal language
must be justified by the source's design, numeric claims must verify with no
direction flips, the narrative must hold, and the manifest integrity audit must
pass. The function returns the verdict and the list of failing clause names, so
a citation that does not converge arrives with its reasons attached rather than
being dropped. Overall confidence is the minimum across contributing agents,
not an average.

`ablation.py` replays that rule from the persisted trail with one or more agents
forced to abstain, which measures what each agent actually contributed. It
issues no new LLM or API calls.

## Tests

42 tests, all passing, observed with `python3 -m pytest` on Python 3.14:

```
42 passed, 1 warning in 0.26s
```

They assert that all 58 agents register across the expected 10 waves with no
name collisions, that `Agent.run()` cannot return a value that skipped its
verifier, that the convergence rule gives the right verdict for converged,
conflicting, partially corroborating and abstaining inputs, that the schema
layer round-trips, and that the deterministic agents with real logic behave.
The single warning is pytest reporting an unknown `asyncio_mode` option, from
`pytest-asyncio` not being installed.

## Scope limits

- **The pipeline needs model access.** The LLM waves run either against the
  Anthropic API, when `ANTHROPIC_API_KEY` is set, or as a subprocess against
  the Claude Code CLI. Set `CITE_FORGE_LLM_BACKEND` to `anthropic` or
  `claude_cli` to choose explicitly. A build run costs real money or real
  subscription usage; the deterministic layers, and the test suite, do not.
- **`build` is the only wired command.** `extract`, `fetch`, `verify`,
  `manifest` and `replay` exist in the CLI surface and raise
  `NotImplementedError`. `ingest` lists and hashes newly added PDFs but does not
  yet re-run A27 verification on them.
- **Retrieval is open access only.** The cascade covers Unpaywall, Europe PMC,
  PubMed Central, preprint servers and publisher OA endpoints. For anything
  paywalled, A25 emits a clickable institutional-proxy login URL that a human
  opens in a browser. `cite_forge/proxy.py` generates URLs and nothing else: it
  never authenticates, never sends a request to the proxy, never stores a
  credential or cookie, and never bulk-downloads. That boundary exists because
  systematic downloading violates library license terms and can suspend access
  for an entire institution. The proxy host is currently hard-coded to the
  University of Miami libraries and would need changing for another institution.
- **Retrieved PDFs are third-party copyrighted files.** They are written next to
  the manuscript, outside this repository, and `.gitignore` excludes `*.pdf` and
  `papers/` so a corpus cannot be committed by accident.
- **No judgment is delegated to this tool.** A converged citation means a
  specific list of mechanical checks passed. It is not a finding that the claim
  is correct or that the source supports it as used. That read is the human's,
  and the escalation packets exist to make it a short one.
- **Polite-pool contact.** Outbound requests to CrossRef, Europe PMC and
  Unpaywall identify themselves with a contact email set in
  `cite_forge/http.py` and `cite_forge/agents/W5_retrieval.py`. Change both to
  your own address before running the pipeline.

## Requirements

- Python 3.11 or newer
- `pip install -e .` for the runtime dependencies (`httpx`, `pydantic`,
  `tenacity`, `anthropic`, `click`, `rich`, `pypdf`, `python-docx`,
  `aiolimiter`, `lxml`, `rapidfuzz`)
- `pip install -e '.[dev]'` for `pytest`, `ruff`, `black` and `mypy`
- Either `ANTHROPIC_API_KEY`, or the Claude Code CLI on the path, for the LLM
  waves
- Network access for the identity and retrieval waves

## License

MIT. See [LICENSE](./LICENSE).
