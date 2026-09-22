# Design decisions, and how to defend them

Every significant choice in this system, with the reasoning, what was rejected, and the
evidence behind it. Written to be read end to end before explaining the project to someone
else.

- **What it does and how**: [PIPELINE.md](PIPELINE.md)
- **What changed and when**: [IMPROVEMENTS.md](IMPROVEMENTS.md)
- **This file**: *why*, plus the questions a sceptical reader will ask.

Every number below was measured on this machine, against a real 133-page Macron sports
catalogue (38 MB) or the generated 120-page sample, not estimated.

---

## The problem

Given a customer (age, income, family size, interests, location) and a product catalog PDF,
produce a personalised A4 brochure: pick the products that suit them, write copy about those
products, and render a PDF. The hard part is not generation - it is **not lying to the
customer**, because every sentence ends up in a document a salesperson hands over.

---

## Architecture

### D1. One Python service, not Go + Python
**Decision.** A single FastAPI service. The original Go orchestrator plus Python "AI sidecar"
was removed.

**Why.** Four of the nine Go steps were HTTP clients forwarding to Python, and most of the rest
was LLM plumbing (JSON cleanup, retries, fallbacks) that LangChain's structured output already
does. The split caused real bugs: API keys had to be set in two `.env` files (a documented
incident), and Pydantic schemas were hand-synced with Go structs that silently zero-filled on
mismatch.

**Alternatives.** Keep Go for the orchestrator (rejected: every request is LLM-latency-bound,
so Go's speed buys nothing); Go calling an LLM library directly (rejected: the ecosystem for
structured output and retrieval is Python).

**Evidence.** ~3,200 lines of Go became ~1,500 lines of Python with more functionality.

**Trade-off.** Lost a polyglot showcase. Kept the ideas: the workflow runner still does ordered
steps, per-step retries and rollback - in 80 lines instead of 210.

### D2. A hand-written workflow runner, not LangGraph/Celery/Temporal
**Decision.** `app/pipeline/workflow.py`: steps, retries with backoff, compensation.

**Why.** The pipeline is a fixed linear sequence of six steps. A graph framework adds a
dependency and a mental model for branching that does not exist here; a task queue adds a
broker and a worker for a single-user app.

**Revisit when** steps need to fan out or run conditionally, or when work must survive a
restart.

### D3. Single user, on purpose
**Decision.** One catalog at a time, in-process job registry, no auth.

**Why.** Explicitly scoped by the owner. It removes per-user isolation, a database and a queue.

**Consequence, stated plainly.** Concurrent users would overwrite each other's catalog. The
code says so where it matters, and `docs/IMPROVEMENTS.md` records it as out of scope, not
forgotten.

---

## Retrieval and extraction

### D4. Retrieval per interest, not one blended query
**Decision.** One vector search per hobby ("Gear and equipment for camping"), merged by block.

**Why.** Catalog text describes products, not customers, so putting income or family size in
the query adds vocabulary the catalog never uses. And a single blended query lets one interest
dominate.

**Evidence.** A customer with "basketball, running" retrieves basketball blocks *and* athletics
blocks; the merged set feeds ranking.

### D5. Product-sized blocks, not whole pages
**Decision.** Pages are split at vertical gutters, then at vertical gaps, using word
coordinates (`app/rag/layout.py`).

**Why.** Measured on the real catalogue: **71% of pages hold several products**. One chunk per
page meant four products shared one index entry and one photo, and the extractor received a
blob. Reading in stream order also merged columns:
`SOFTLOCK + MESH: 100% POLYESTERAvailable until 202880000274`.

**Alternatives.** Fixed-size overlapping chunks (rejected: cuts products in half and ignores
the layout that makes a catalog readable); a layout ML model such as LayoutLM (rejected:
heavyweight for a rule that geometry expresses directly).

**Evidence.** 644 blocks from 122 pages, median 6 per page. Page 54's four products (GOLEM,
WISP, WYVERN ECO, BISMUTH) are now separate, and **prices the flattened text had buried
(`UVP € 33,99`) became visible**.

**Trade-off.** More blocks means more embedding requests (see D14) and ~0.8 s/page of parsing.

### D6. Support blocks are merged, non-product blocks dropped
**Decision.** Colour/size blocks are folded into the nearest product block; blocks with no
price and no product code are not indexed - unless fewer than 30% of blocks look like products,
in which case everything is kept.

**Why.** Colours live in their own swatch rows in this catalogue, so products had empty colour
lists. And covers, index pages and brand stories cost an embedding request each and compete in
search - a live run matched the cover page.

**Evidence.** 644 → 533 blocks after merging → 445 indexed. 53% of product blocks now mention
colours. The 30% guard means a catalog that prints no prices is not thrown away.

### D7. Extraction refuses to invent
**Decision.** The extraction prompt requires: unstated price → `0`, unstated specs omitted,
pages without products skipped, empty result allowed.

**Why.** The prompt used to say *"make a highly accurate estimate"* and *"never return an empty
array"*. Invented prices were printed in customer-facing brochures, and since the critic audits
copy against extracted specs, invented specs became the "truth" the audit trusted.

**Consequence.** A price of 0 renders "Price on request" rather than "$0.00".

### D8. Extraction results are cached per block
**Decision.** Cached against the catalog's content hash, cleared when the catalog is re-indexed.

**Why.** The same popular blocks match run after run, and extraction is the largest prompt in
the pipeline.

**Evidence.** A repeat run went from 6 misses to 6 hits: 38 s → 31 s and one LLM call fewer.

### D9. Product photos are cropped from the rendered page
**Decision.** For each block, take the image whose vertical span overlaps it, nearest
horizontally, and crop that region out of the page render as JPEG.

**Why.** Pulling the embedded image stream gives encodings a browser may not display, and no
reliable link to the product. "Largest image on the page" is wrong when a page has 19 of them.
Catalogs put the photo *beside* the text: on page 31 the balls sit at x −14…170 while their
text starts at x 175 - an "inside the block" test found nothing.

**Evidence.** 207 → **594 of 644 blocks** with their own photo. JPEG at quality 82: 88 KB →
20 KB per crop.

---

## Copy quality and trust

### D25. Hybrid retrieval: vector + BM25, fused by rank
**Decision.** Every query runs both a vector search and a BM25 keyword search over the indexed
blocks; the two rankings are combined with reciprocal rank fusion (`1/(k+rank)`, k=60).

**Why.** Vector search is good at meaning and bad at identifiers - an embedding of `80000274`
sits near every other product code - while BM25 is the reverse. A catalogue is full of codes and
brand names ("TurboWash", "WYVERN ECO"), so the two engines cover each other's blind spot.

**Why rank fusion rather than score blending.** A cosine similarity and a BM25 score are not on
comparable scales; normalising them would invent a relationship that does not exist. Ranks are
comparable by construction.

**Trade-off.** The keyword index is held in memory and rebuilt when the catalog changes (~450
blocks, trivial). A BM25 score is deliberately *not* returned in the `score` field, which means
cosine similarity everywhere else - a keyword-only hit shows as "keyword" in diagnostics instead.

### D10. Deterministic checks first, LLM critic second
**Decision.** Every draft is checked by code (terms absent from the specs; banned words) before
any LLM review, and the LLM reviews are skipped entirely when the code has already rejected it.

**Why.** In a live run the LLM critic **approved copy promising a "SmartThings app"** for a
fridge whose specs mention no app - twice, and the second draft shipped. Critics catch
contradictions; they miss plausible additions. The deterministic check also means copy is never
completely unreviewed when the critic is unavailable.

**How it avoids false positives.** It only flags specific-looking terms: internal-capital names
(SmartThings, ThinQ), acronyms (NFC), and numbers. Comparison ignores case, spacing,
punctuation and Unicode hyphens ("WiFi" matches "Wi-Fi"), and short terms must match a whole
spec word because "ai" is a substring of "stainless". Run over two real drafts it flagged
exactly the invented app and a leaked internal match score, with no false positives.

**Known limit.** Plain-language additions ("get alerts on your phone") still depend on the LLM
critic.

### D11. Rejected copy is revised, not re-judged
**Decision.** A rejection returns to the *writer* with the feedback, at most twice; still
rejected → claim-free generic copy, reported as a warning.

**Why.** The previous system re-ran the *critic* on the same copy, so a non-deterministic model
eventually passed it. That is rejection-sampling the judge, not fixing the text.

### D12. Ranking reasons are fact-checked too, against specs **and** the customer
**Decision.** Each "why this fits" line is grounded against the product's specs plus the
customer profile; unsupported ones are dropped, a bad explanation is replaced.

**Why.** Those lines are printed in the brochure. They legitimately cite the customer ("fits
your family of 3"), so the customer profile counts as a source of truth - otherwise every
correct personalised reason would be flagged.

### D13. Every fallback is reported
**Decision.** Each degradation appends to `warnings`, returned by the API and shown in the UI.

**Why.** The Go version fell back silently at nearly every step - canned profile, fake catalog,
auto-passed critic, invalid PDF - while reporting `success: true`. A brochure built from
generic copy is not a failure, but the operator must know.

---

## Performance and cost

### D14. Fewer LLM calls, because latency is the cost
**Decision.** Profile, planner and tone evaluator are off by default (switchable). Critic and
evaluator run in parallel. A typical run makes 3-5 calls, down from 7.

**Why, with numbers.** A whole run is **~5,400 tokens** - trivially cheap. The cost is
round-trips. So the question for each call is not "what does it cost?" but "does it change the
output?":
- *Profile*: rules use the same inputs, instantly, and cannot invent a segment.
- *Planner*: its outline only ever fed the writer, which can structure copy itself.
- *Tone evaluator*: its verdict never affected approve/revise - only the banned-word scan and
  the spec checks do.

**Evidence.** Input tokens 4,020 → 2,581 per run with quality signals unchanged (0 generic
copy, 0 unsupported claims). Copy step 100.7 s → 31.7 s across the model switch plus parallel
review.

**Defensible because reversible.** Each is a documented switch (`USE_LLM_PROFILE`,
`USE_LLM_PLANNER`, `USE_LLM_TONE_EVALUATOR`), so the claim can be re-tested with
`scripts/benchmark.py`.

### D15. One call for all per-product blurbs
**Decision.** Options 2-4 get a fact-checked sentence each, generated in a single call.

**Why.** A call per product would triple the slowest step. A blurb that fails its grounding
check is dropped rather than revised: it is one sentence beside a spec list, so losing it costs
little, while another round trip costs the customer ten seconds.

### D16. Two providers, automatic failover
**Decision.** Gemini `gemini-3.1-flash-lite` primary, OpenRouter free model as backup; a failed
call is retried once on the backup, and the run says when the backup answered.

**Why.** Free tiers return 503 and 429 constantly, and a failed call costs a step its output.
Switching to another free model on the *same* vendor does not help - the daily cap is per
account.

**Evidence.** Before failover, 1 of 3 benchmark runs fell back to generic copy; after, 0 of 3.

---

## Reliability

### D17. Ingest is versioned and non-destructive
**Decision.** An index is reused only when the PDF hash *and* `INGEST_VERSION` match. Ingest
builds into its own collection and switches over only once chunks are indexed.

**Why.** Both learned the hard way. Reuse by content hash alone served an index built by the
*old* chunking forever, fixable only by a manual rebuild. And deleting collections up front
meant that when the embedding quota ran out mid-ingest, the working catalog was already gone.

### D18. Each catalog gets its own collection
**Decision.** Collection named from the catalog's content hash.

**Why.** `qdrant-client` 1.9 in on-disk mode does not really drop a deleted collection's
points: after delete + create they are still there. Reusing one name left 884 points indexed
for a 644-chunk catalogue, 240 of them from a previous catalog - which could surface as
recommendations.

**Alternative.** Upgrade the client (not attempted: a fresh name is one line and cannot regress).

### D19. Rate limits: wait for a per-minute cap, fail fast on a per-day cap
**Decision.** Retry rate-limited embedding batches using the provider's own `retry_delay`
(ingest: up to 4 times; a live query: once, ≤10 s). A per-day cap is not retried.

**Why.** A per-minute cap clears in a minute, and a catalog whose later pages hold mock vectors
is silently broken for the rest of its life - worth waiting for. A per-day cap does not clear
today. Queries are different again: a user is waiting, so it degrades and says so.

**Evidence.** A 120-page ingest waited 29 s then 60 s and completed with real embeddings; the
same case previously left those pages on mock vectors.

### D20. No mock PDF, no silent stock photo
**Decision.** If rendering fails, the job fails. If a product has no photo, the page shows a
branded panel - not a stock photo of something else.

**Why.** The Go version wrote a 40-byte "PDF" and reported success. And the stock fallback put
a kitchen photo in a sports brochure, which reads as a mistake to the customer.

### D21. PDF rendering runs in a child process
**Decision.** `python -m app.render.pdf <html> <pdf>` via Playwright.

**Why.** On Windows, uvicorn's reload mode installs an event loop under which Playwright cannot
spawn a browser; a child process gets a clean interpreter, and a hung or crashed browser cannot
take the server with it.

---

## Testing and measurement

### D21b. One job per module in `app/rag/`
**Decision.** `embeddings.py` (text → vectors), `blocks.py` (PDF → product blocks),
`index.py` (collections and the catalog record), `search.py` (the ingest sequence and queries).

**Why.** `search.py` had reached 659 lines doing all four, with a 121-line `ingest_pdf` - and
both the stale-collection bug (D18) and the destructive-ingest bug (D17) lived in it. Code you
cannot hold in your head is where those bugs come from.

**Trade-off.** Four files to open instead of one, and a facade that re-exports nothing - callers
import from the module that owns the job.

### D22. Tests never touch a real provider
**Decision.** An autouse fixture stubs the single call point for every test; storage, keys and
PDF rendering are redirected or disabled.

**Why.** Tests must not be slow, flaky, or spend quota. When brand detection was added it
started calling the live API during the suite and tripled its runtime - hence one enforced
patch point rather than per-module patching.

**Evidence.** 80 tests, ~10 s, no keys needed.

### D23. Speed claims come from a benchmark, not from a stopwatch
**Decision.** `scripts/benchmark.py` runs fixed profiles, records per-step and per-call times
plus tokens, and reports quality signals a speed change must not worsen: revisions,
generic-copy fallbacks, other fallbacks, and unsupported terms in the copy that shipped.

**Why.** Free-model latency is extremely noisy - one run varied 54 s to 207 s because of a
single slow call. A speed change is easy to "prove" by accident.

**Honesty rule applied.** One comparison came out *worse* (54.6 s → 130.4 s) because of mock vs
real embeddings and rate-limit waits; it is recorded as invalid rather than dressed up, and the
call/token reductions - which are deterministic - are quoted instead.

### D24. Logic that must not depend on a provider is tested offline
**Decision.** The parallel review and skip-the-critic behaviour are proven with fake LLMs that
sleep, asserting wall time is ~1× not 2× the delay.

---

## Numbers worth remembering

| | |
|---|---|
| Real catalogue | 133 pages, 38 MB, 0 pages needing OCR, 71% multi-product pages, ~19 images/page |
| Indexing | 644 blocks → 445 after merge + filter; ~8 min (quota-bound), once per catalog |
| A run | 3-5 LLM calls, ~5,400 tokens, ~30-120 s |
| Copy step | ~80% of run time; writer ~16 s/draft |
| Quality | 0 unsupported claims in shipped copy across every benchmark run |
| Free tiers | 100 embeddings/minute, 1,000/day; chat models 503/429 frequently |
| Tests | 80, ~10 s, no API keys |

---

## Questions you should expect

**"How do you know the brochure isn't making things up?"**
Three layers, and the middle one exists because the first failed in production: extraction may
not invent (D7); a deterministic check rejects specific terms absent from the specs (D10); an
LLM critic reviews what remains and rejections go back to the writer (D11). Ranking reasons get
the same treatment (D12). If copy cannot be verified, the brochure uses claim-free text and the
run reports it (D13). Evidence: the LLM critic alone approved an invented "SmartThings app"
twice; the deterministic check catches it.

**"Why RAG and not just send the PDF to the model?"**
A 133-page catalogue is far past a comfortable prompt, it would be re-sent on every run, and
the customer needs the 6 relevant blocks, not 133 pages. Retrieval also gives a traceable
answer to "why this product?" - the UI shows which pages matched and with what score.

**"Why not OCR?"**
Because it was measured first: 0 of 133 pages lack extractable text, so OCR would have bought
nothing here. It is needed for scanned catalogs, and is in the backlog for that case. Guessing
would have wasted days.

**"Why free models - would you ship this?"**
It is a build-time cost decision; provider, model and backup are three config values, so
production means changing `.env`. The architecture is what makes that safe: one call path, one
place where provider choice lives (`app/ai/llm.py`).

**"Why no database?"**
Nothing needs relational queries: the catalog index is Qdrant, the catalog record is one JSON
file, generated files are on disk, and jobs are per-process and short-lived. A Postgres schema
existed in the Go version and was unused - it was deleted rather than carried.

**"What breaks at 100 users?"**
The single shared catalog and the in-process job registry, first. That is a stated scope
decision (D3), not an oversight; the fix is per-user catalogs (collection per user + catalog)
and moving jobs to a store outside the process.

**"What's the weakest part?"**
The copy step is ~80% of run time and the writer is the single biggest call. After that:
retrieval fuses vector and keyword search but never re-ranks, so a cross-encoder over the fused
top-20 is the obvious next quality step.

**"What would you do with a budget?"**
Paid embeddings (removes the 1,000/day ceiling and most of the 8-minute ingest), a stronger
model for the critic only (`LLM_CRITIC_MODEL` exists for exactly this), and a fixed evaluation
set of catalogs so quality changes are measured rather than argued.

---

## Known limitations

Kept deliberately visible rather than hidden:

- Single user; concurrent use corrupts the shared catalog (D3).
- No OCR: a scanned catalog indexes nothing (it warns).
- Vector-only retrieval; exact model codes match poorly.
- The writer's cover copy is based on the top product only; other products get one checked
  sentence each (D15), not full copy.
- `/storage` is served publicly; job ids are unguessable but the folder is not access-controlled.
- Free-tier quotas cap re-indexing at roughly two catalogs a day.
- Retrieval fuses two engines but does not re-rank; a cross-encoder would likely beat both.
- The customer's name is optional and used only on the cover; nothing else is personalised by it.
