# Decision Log

A short record of notable technical decisions made during development.
Add one entry per decision, most recent first.

## Template

- **Date**:
- **Decision**:
- **Problem**:
- **Alternatives**:
- **Trade-off**:
- **Outcome**:

---

- **Date**: 2026-08-27
- **Decision**: Schedule the roadmap by distributing DAYS
  (`roadmap_schedule_v1`), derive the week count from the declared
  duration, and bound `top_n` by the user's actual saved-job count.
- **Problem**: Browser testing found three things the first cut got
  wrong. Four fixed phases with day-range LABELS told a candidate with
  fourteen days they had four weeks. Distributing HOURS could round each
  item up past the declared window at low hours-per-day. And the
  fixed Top-3/5/7/10 options had no relationship to how many jobs the
  user had actually saved.
- **Alternatives**: (1) keep four phases and label them harder;
  (2) distribute hours and clamp the overflow; (3) a static `le=` bound
  on `top_n`.
- **Trade-off**: Distributing days is what makes the window EXACT rather
  than approximately right: `divmod(duration_days, n)` sums to
  `duration_days` by construction, so there is no overflow and no
  unclaimed tail, and `estimated_hours` follows from the span instead of
  being computed separately and hoped to agree. The cost is that an item
  needs at least one day, so the item count is additionally bounded by
  the duration — without that a twelve-item budget over seven days
  divides to a span of zero. The remainder goes to the highest-ranked
  items; somebody has to get the extra day.
- **On the dynamic bound**: `top_n`'s ceiling is a per-request fact and
  cannot be a static Query bound, so the floor is declared and the
  ceiling checked in the handler. Two cases matter and both were found
  by testing rather than by reading: a user with ZERO saved jobs must
  not get a 422 (any `top_n` exceeds zero, but "you have not saved any
  jobs yet" is an empty state, not a client mistake), and an OMITTED
  `top_n` resolves against the real count instead of erroring — a
  default of 5 that the user never typed must not be able to be invalid
  for somebody with one saved job.
- **On presentation**: the item card now answers what to do, what you
  end up with, and who it helps, in that order; `why`, the full job
  list, the capability check and the score terms moved behind native
  `<details>`. `outcome` (an artefact) is a new narrative field distinct
  from `success_criteria` (a capability) — "a running service and a
  README" versus "you can explain why you chose it". A plan with only
  the second is a reading list. Weekly `checkpoint` is phrased as an
  observable capability because "did you finish it" and "can you now do
  it" are different questions.
- **On the explanation**: the mock emitted "Your stored evidence covers
  X" once per matched skill, which read as a database dump. It now
  writes one strength naming them together, and the excerpts moved to a
  single "Supporting evidence (N)" disclosure, deduplicated. Every
  excerpt is still a stored row and all of them remain available — proof
  belongs where a reader reaches for it, not interleaved through the
  first paragraph they read. No validator rule changed.
- **Outcome**: `roadmap_priority_v1` is byte-for-byte unchanged and a
  test asserts scheduling does not re-rank. Weeks are now derived: 28
  days is still four, 14 is two, 56 is eight — a departure from the
  brief's literal "four-week roadmap" that keeps its canonical case
  exact.

- **Date**: 2026-08-27
- **Decision**: Rank the learning roadmap on the USER's saved-job order
  plus gap kind (`roadmap_priority_v1`), and keep `skill_match_v1` out
  of the arithmetic entirely.
- **Problem**: "Which jobs matter most" has an obvious wrong answer —
  the highest match score. That inverts the candidate's stated intent:
  a job they ranked FIRST gets demoted precisely because they match it
  poorly, which is also the situation with the most to learn.
- **Alternatives**: (1) rank by `skill_match_v1`; (2) blend it with user
  order; (3) use it as a final tie-break.
- **Trade-off**: All three were rejected on the same ground — the
  direction is not merely debatable, it is genuinely ambiguous. A low
  match means more preparation is needed, so any weight picks a side the
  data does not support. The score is instead DISPLAYED per affected job
  ("#1 Acme — Engineer, 62% skill match"), where a person can weigh it
  themselves. As a tie-break it would fire rarely and be hard to explain
  when it did.
- **The load-bearing constant**: recurrence is capped at 24, one less
  than the narrowest gap between two state weights (50 - 25). That is
  what makes the bands non-overlapping: required 100-124, preferred
  50-74, weak 25-49. Uncapped, a merely-preferred skill wanted by fifty
  saved jobs reaches 5+4+3+2+46 = 60 and overtakes a required one — a
  failure that appears only at scale, long after small fixtures pass. A
  test asserts `MAX_RECURRENCE < min(band gaps)` directly, and three
  parameterised cases stress it at 5, 10 and 50 jobs.
- **On weak evidence**: defined as `skill_gap_v1`'s own
  `needs_confirmation` bucket, so the roadmap's idea of "weak" cannot
  drift from what the gap panel shows. Deliberately NOT `confidence`,
  which is match quality rather than capability — ranking a study plan
  by it would make exactly the claim `score.py` and `schemas/skill.py`
  both document against. Rejected requirements never become items: the
  user disowned that skill.
- **On position**: it ORDERS, it does not LABEL. The rank a user sees is
  the 1-based index in the sorted response, so a deletion leaves a gap
  nobody has to repair. Unique per user and DEFERRABLE INITIALLY
  DEFERRED — a reorder rewrites a list in one transaction and
  legitimately holds duplicates until COMMIT; the usual workaround
  (shuffle everything negative first) doubles the writes to dodge a
  check that belongs at the end.
- **On persistence**: only `position` is stored. Top-N and time are
  query parameters, because the line worth holding is "priority is data
  about the user's jobs; top-N and hours are arguments to one request".
  The roadmap itself is derived on read for a sharper reason than the
  usual cache-invalidation one: a stored plan asserting "AWS is required
  by 3 of your top 5 jobs" in a SENTENCE becomes a false claim about the
  user's own data the moment two of those jobs are deleted, and unlike a
  stale number a stale paragraph still reads like a fact.
- **On the LLM boundary**: the narrative is a MAP keyed by deterministic
  `item_id`, not a list. Adding a skill, dropping one or reordering the
  plan are not violations to catch — the response shape has nowhere to
  express them. 6.2's timeout/retry/logging moved to
  `app/explanation/runtime.py` and is called by both adapters unchanged,
  rather than copied.
- **Stated rather than hidden**: `HOURS_PER_ITEM` and every
  `estimated_hours` are planning assumptions. CareerLens has no idea how
  long it takes anyone to learn AWS, and the schema, the API docstring
  and the UI all say "estimated" rather than implying a measurement.
- **Outcome**: one migration (`saved_jobs.position`), one additive
  response field, and one behaviour change — `GET /saved-jobs` now
  orders by the user's order rather than newest-first, seeded so the
  first render after migrating is identical to the last one before it.

- **Date**: 2026-08-27
- **Decision**: Retry the LLM explanation's TRANSPORT and never its
  CONTENT: one retry on timeout/unavailable, none on any validation
  failure, with a 10s per-attempt budget applied by the adapter.
- **Problem**: 6.1 had no timeout (a hung provider hangs a request-path
  route), no retry (one blip costs the user their explanation), an
  untyped `except Exception` that could not tell transient from
  terminal, and no logging at all — so a rejection was undiagnosable.
- **Alternatives**: (1) retry everything until something validates;
  (2) exponential backoff; (3) a timeout inside each provider;
  (4) log the rejection detail to debug groundings.
- **Trade-off**: Retrying a validation failure is the tempting one and
  the wrong one. It spends money to re-roll a dice the user does not
  need rolled — they already have the deterministic score — and it
  turns one hallucination into a loop that eventually gets lucky and
  SHOWS one. So `return` on `ExplanationRejected` is the whole policy,
  and `attempts` is on the outcome purely so a test can prove the call
  count was 1. Exponential backoff with two attempts is decoration.
  The timeout lives in the adapter, not the provider, so a future
  client library that forgot its own deadline still gets one.
- **On logging**: the detail field quotes model output and the excerpts
  are somebody's resume, so `adapter.py` is the ONLY module in the
  feature that logs — one file to audit rather than a habit spread over
  six. It records job id, provider, attempt, reason and elapsed ms, and
  `type(error).__name__` rather than `logger.exception`, because a real
  client's traceback can carry the request body and a stack trace is
  exactly where an excerpt reappears unnoticed. A parameterised test
  runs the whole path under `caplog` and asserts the excerpt, the model
  output, the title and the company are absent.
- **On the input bound**: nothing capped the fact bundle, so a prompt
  grew with a candidate's import history. Capped per skill (3) BEFORE
  ids are built — a fact citing a row the cap dropped is
  indistinguishable from an invented citation — plus a 40-row backstop.
- **The honest part**: the `<untrusted_data>` markers are defence in
  depth, NOT the boundary. JSON encoding is what stops an excerpt
  escaping its own string, and the test asserts that structural
  property directly instead of trusting the tags. The markers exist for
  a provider that receives instruction and data concatenated.
- **Outcome**: reason values `provider_timeout` / `provider_unavailable`
  / `provider_error` joined `RejectionReason` so `reason` has one
  vocabulary. Route contract, score, gaps, eligibility and semantic fit
  unchanged; the job description still never reaches the model, pinned
  by its own test.

- **Date**: 2026-08-27
- **Decision**: Ground the 6.1 LLM explanation by CONSTRUCTION on the way
  in and by VALIDATION on the way out, and return a rejection as a
  200 with `status`/`reason` rather than an HTTP error.
- **Problem**: An LLM asked to explain a match will happily invent a
  skill, a requirement or a number, and prose is exactly the surface
  where an invention is most believable. docs/project-brief.md's
  Evidence-First rule says it may explain persisted results and never
  invent — which needs a mechanism, not an instruction in a prompt.
- **Alternatives**: (1) trust the system prompt; (2) filter the bad
  claims out and keep the rest; (3) 502 on an invalid response;
  (4) persist explanations.
- **Trade-off**: Prompt wording depends on a model complying, and a
  guarantee that depends on compliance is not one — so the input schema
  has NO field for a resume, README or job description (raw text cannot
  leak because there is nowhere to put it), and the output is checked
  against the facts it was given: unknown evidence id, uncited strength,
  out-of-facts taxonomy skill, or a number absent from the facts all
  reject the WHOLE answer. Filtering was rejected because "we removed
  the parts we could detect" is not a claim worth making. A 502 was
  rejected because the deterministic score is intact and worth serving —
  the model failing to explain it is not an error in the match — so
  `status: "rejected"` plus a machine-readable `reason` carries the
  outcome with no generated content attached.
- **The limits, stated rather than hidden**: the number check asks only
  whether a quantity appears in the facts at all, so "3 years" survives
  when 3 is a requirement weight; the skill check can only see names in
  the ~33-entry curated taxonomy and is case-sensitive (so it does not
  reject "go through your gaps"); and a fluent sentence built from
  in-vocabulary words is not mechanically detectable. Requiring a
  citation on every strength is what bounds that residue. All three are
  written into app/explanation/validate.py's docstring and pinned by a
  test.
- **Outcome**: `/explanation` calls the existing `/match`, `/gaps` and
  `/semantic` handlers directly rather than recomputing, which makes
  "the score is unchanged" the same code path instead of a second
  implementation that could drift — and a test asserts both responses
  are byte-identical around an explanation request. Mock provider only;
  no credential, no hosted provider, no persistence, no migration.

- **Date**: 2026-08-27
- **Decision**: Turn the 5.3 harness into a regression policy with a
  checked-in `baseline.json`, simple metric-delta rules, and NO
  third-party CI dependency.
- **Problem**: The evaluation produced numbers nobody was obliged to
  look at again. Without a baseline, a change that degrades retrieval
  merges silently.
- **Alternatives**: (1) statistical significance testing; (2) a separate
  path-filtered CI workflow via `dorny/paths-filter`; (3) storing the
  baseline outside the repo.
- **Trade-off**: Twelve synthetic cases cannot support a significance
  test, and a policy nobody can reproduce mentally is one people learn
  to override — so the rules are absolute deltas argued from the 5.3
  measurements (0.02 ~= one case of twelve; actual-to-random gap is
  0.18 at NDCG@5). On CI: GitHub Actions filters paths per WORKFLOW, not
  per job, so per-job filtering needs a third-party action — and it
  would buy nothing, because the model-free checks cost milliseconds.
  They run in the existing pytest step instead.
- **The limitation, stated rather than hidden**: CI has no model
  weights, so it verifies fingerprints and config only. A change to
  `app/embeddings/retrieval.py` moves ranking without moving any
  fingerprint, so CI passes while validating nothing about it. That
  requires a local `make evaluate-check`, and it is written into
  `policy.py`, the README and a test that asserts the docstring still
  says so.
- **Outcome**: Baseline + policy landed. Putting `config` (FLOOR / CEIL
  / TOP_K) in the baseline is what makes a threshold change CI-visible
  without a model. Re-baselining must happen in the same PR as the
  change that caused it; a standalone baseline commit is
  indistinguishable from accepting a regression.

- **Date**: 2026-08-27
- **Decision**: Measure semantic retrieval against a 12-case synthetic
  benchmark (Prompt 5.3), and change NO production threshold on the
  strength of it.
- **Problem**: `semantic_fit_v1` shipped with FLOOR=0.20 / CEIL=0.60
  derived from a seven-sentence fixture, explicitly labelled provisional
  and owed a real measurement.
- **Findings** (`retrieval-eval-v1`, 12 cases / 56 items,
  all-MiniLM-L6-v2, `make evaluate-retrieval`):
  - Ranking is sound: NDCG@3 0.923, NDCG@5 0.972, Recall@5 1.000,
    coverage 12/12. Precision@5 (0.517) exactly equals its own ceiling,
    so every relevant item that could be in the window is in it.
  - **FLOOR=0.20 is well placed for the relevant/unrelated boundary**:
    31/31 relevant items admitted, 0/14 unrelated admitted. The margin
    is thin — unrelated max 0.186 against relevant-paraphrase min 0.232.
  - **CEIL=0.60 is effectively unreachable for paraphrases**: only 1 of
    19 clears it, while lexical overlap reaches 0.825. A candidate whose
    evidence matches in different words is capped near fit 15/20 while
    keyword overlap saturates at 20 — the formula currently rewards the
    surface overlap semantic search exists to look past.
  - **No threshold separates lexical distractors**: 9/11 clear the
    floor, and their range (0.151-0.529) overlaps relevant paraphrases
    (0.232-0.613) almost entirely.
  - Duplicate crowding is real but small: 3 top-5 slots lost across 12
    cases.
- **Trade-off**: The benchmark is authored by the same person building
  the feature, so queries and evidence were written together — a known
  bias. It is fit for catching direction-of-travel regressions and
  unfit for any claim about real-world hiring accuracy.
- **Outcome**: Harness landed, thresholds untouched. Changing CEIL and
  handling lexical distractors are proposals for a later reviewed slice.

- **Date**: 2026-08-27
- **Decision**: Adopt `fastembed` running
  `sentence-transformers/all-MiniLM-L6-v2` as the real local embedding
  provider, rather than the official `sentence-transformers` package.
- **Problem**: 5.2b needs real semantic embeddings at 384 dimensions,
  locally, with no hosted API and no credential.
- **Alternatives**: (1) `sentence-transformers` (PyTorch); (2)
  `BAAI/bge-small-en-v1.5` as the model.
- **Trade-off**: Measured in throwaway venvs — current 196 MB, fastembed
  340 MB (+144), sentence-transformers 954 MB (+758, of which torch
  alone is 490 MB). Both run the SAME model and produce the same 384-dim
  output, so this is 5.3x the disk for no accuracy difference. On the
  model choice, MiniLM beat BGE on a 7-sentence fixture: related-minus-
  unrelated margin +0.0699 vs +0.0259, and MiniLM's floor is near zero
  (unrelated 0.124 / -0.015) where BGE compresses everything into
  0.50-0.77, leaving no interpretable threshold. BGE's prescribed query
  prefix made it worse still — it INVERTED the ordering, ranking an
  unrelated item above a related one.
- **Outcome**: fastembed + all-MiniLM-L6-v2. 384 dimensions, so 5.1's
  `vector(384)` needs no migration — the width chosen in 5.1 precisely
  because this was the likely first real provider.

- **Date**: 2026-08-27
- **Decision**: `semantic_fit_v1` is reported on its own endpoint
  (`GET /saved-jobs/{id}/semantic`) and is NEVER folded into
  `overall_score`.
- **Problem**: 5.2b adds a semantic signal, and the obvious move is to
  blend it into the headline match percentage.
- **Alternatives**: (1) add a field to the `/match` response; (2) blend
  into `overall_score` as `0.8*v1 + fit`, or `min(100, v1 + fit)`.
- **Trade-off**: A separate endpoint costs the client a second request.
  In exchange `skill_match_v1` is unchanged *structurally* rather than
  merely by test, and a cold, unconfigured or failing model cannot delay
  or break the deterministic score. Blending was rejected outright:
  `min(100, ...)` gives a strong candidate nothing, and `0.8*v1 + fit`
  silently redefines the baseline every stored screenshot was taken
  against.
- **Outcome**: Separate endpoint, separate version string, separate UI
  panel placed below both the score and eligibility.

- **Date**: 2026-08-27
- **Decision**: Treat `SIMILARITY_FLOOR = 0.20` and
  `SIMILARITY_CEIL = 0.60` as PROVISIONAL and say so everywhere they
  appear.
- **Problem**: The fit formula needs a floor and a ceiling to normalise
  against.
- **Alternatives**: Ship without a threshold; or present the resulting
  number as a calibrated score.
- **Trade-off**: The constants come from a SEVEN-SENTENCE hand-written
  fixture — enough to make the shape of the formula testable, nowhere
  near enough to claim the values are right. FLOOR sits in the measured
  gap between highest-unrelated (0.124) and lowest-related (0.194),
  which is a gap of 0.07 on n=7. Shipping them unlabelled would let a
  reader treat `fit` as a hiring signal it has no basis to be.
- **Outcome**: Constants documented as provisional in the module, the
  API schema and the UI copy; `semantic_fit_v1` is never presented as a
  calibrated hiring or ranking score; validation deferred to 5.3.

- **Date**: 2026-08-27
- **Decision**: Real-provider tests skip when the model cannot be
  loaded; no Hugging Face cache or network step is added to CI.
- **Problem**: The first real embedding downloads ~90 MB, which a cold
  CI runner has no cache for.
- **Alternatives**: Add an HF cache + warm step to `api-ci.yml`.
- **Trade-off**: The related-outranks-unrelated assertion does not run
  in CI, so that specific guarantee is verified locally only. Everything
  else — the formula, ownership, model isolation, the
  baseline-unchanged guarantees — uses the deterministic mock and runs
  everywhere. Accepted to keep CI free of a network dependency and ~30s
  slower for one assertion.
- **Outcome**: `_real_provider()` calls `pytest.skip` on load failure.
  Revisit if the semantic path grows enough to need CI coverage.

- **Date**: 2026-08-27
- **Decision**: Ship Prompt 5.2 as retrieval infrastructure only (5.2a).
  No `semantic_fit_v1` score component, no UI, and no change to
  `overall_score`.
- **Problem**: 5.2 asks for a user-visible semantic-fit score, but the
  only embedding provider is the deterministic mock, whose vectors carry
  no semantic structure. Measured on 5.2's own motivating example:
  "Experience with container orchestration" scores **-0.0372** against
  "Built Kubernetes-based microservices" and **-0.0064** against "Wrote
  marketing copy for a bakery newsletter" — the *unrelated* text ranks
  higher. Over 300 arbitrary pairs the distribution is mean -0.0018 /
  stdev 0.0536, against 0.0510 expected for uniformly random unit
  vectors in R^384. Only exact-text matches rise above the noise.
- **Alternatives**: (1) implement the score behind a disabled flag;
  (2) implement it and display it with mock vectors; (3) adopt a real
  provider first.
- **Trade-off**: The retrieval plumbing is genuinely testable today
  (operator direction, ownership, model filtering, ordering, N+1), so
  building it is not wasted. Displaying a score derived from that
  distribution would put a number on screen that is noise, which
  contradicts docs/project-brief.md's rule that "a score must be
  derived from stored, inspectable signals". Option (2) was rejected on
  exactly that ground; (1) was rejected as code that cannot be
  meaningfully tested and invites being switched on prematurely.
- **Outcome**: 5.2a delivers retrieval and a backfill writer. The score
  component and its UI wait for a real provider.

- **Date**: 2026-08-27
- **Decision**: Add a `cosine_distance` comparator to the existing
  custom `Vector` type rather than adopting the `pgvector` Python
  package, despite the 5.1 decision anticipating adoption "in the slice
  that actually runs a similarity query".
- **Problem**: Retrieval needs the `<=>` operator, which the custom type
  did not expose.
- **Alternatives**: Add `pgvector>=0.5` and use its `Vector` type and
  operators, as the earlier entry expected.
- **Trade-off**: The comparator is ~12 lines against a new dependency,
  and only cosine is exposed — `<->` and `<#>` are deliberately absent
  rather than added speculatively, since an unused operator is one a
  future reader might pick by mistake. The earlier decision is not
  overturned so much as deferred again: the swap remains an import
  change, and the emitted SQL is identical either way. Verified against
  PostgreSQL: identical vectors give distance 0.0, orthogonal 1.0,
  opposite 2.0.
- **Outcome**: Custom comparator; the `pgvector` package stays unadopted
  while the only operator in use is one line of SQL.

- **Date**: 2026-08-27
- **Decision**: Choose NO default similarity threshold.
  `find_similar(min_similarity=...)` defaults to `None`.
- **Problem**: A retrieval API usually wants a relevance cutoff.
- **Alternatives**: Pick a conventional value (0.7, 0.75) as a default.
- **Trade-off**: Callers get unfiltered results and must decide for
  themselves, which is less convenient. But there is nothing to
  calibrate a cutoff against: against the mock's distribution (above)
  any threshold either admits everything or nothing, and a constant
  copied from a blog post about a different model would be numerology
  presented as a tuned parameter.
- **Outcome**: No default. The parameter exists and is honoured when
  supplied; a default arrives with the provider that can justify one.

- **Date**: 2026-08-27
- **Decision**: Persist nothing from retrieval — no similarity table, no
  cached hit rows, no migration in 5.2a.
- **Problem**: Storing retrieved evidence and its similarity would let a
  response be reproduced later without re-running the query.
- **Alternatives**: A `semantic_retrieval_results` table keyed by (job,
  user, model).
- **Trade-off**: Every read re-runs the vector query. In exchange there
  is no staleness problem: a stored similarity is invalidated by new
  evidence, an edited job description, a re-run backfill or a model
  change, and several of those fire from workers outside any request —
  the same reasoning `read_saved_job_gaps` already records for not
  storing gap rows. Nothing is derived that cannot be recomputed from
  the embeddings that are stored.
- **Outcome**: Derived on read. No schema change in this slice.

- **Date**: 2026-08-27
- **Decision**: Fix ONE embedding dimension for the project — **384** —
  as a schema constant (`app/models/embedding.py`'s
  `EMBEDDING_DIMENSION`, and the literal `vector(384)` in migration
  `59ae0cc1cf6b`), rather than supporting several widths.
- **Problem**: pgvector columns are fixed-width, and no prior prompt had
  chosen a dimension. Supporting more than one means either a column per
  width or a dimension-per-row design, and every query then has to know
  which family it is reading.
- **Alternatives**: (1) a nullable/variable `vector` column with the
  dimension stored per row; (2) one table per model family; (3) defer
  the choice and store vectors as `float[]`.
- **Trade-off**: 384 is the width of `all-MiniLM-L6-v2`, the most likely
  first real provider, so the column probably survives that arrival
  unchanged — but a provider with a different width (OpenAI's 1536)
  needs a migration, not a config change. `EMBEDDING_DIMENSION` in the
  environment configures the PROVIDER only; `app/embeddings/store.py`
  raises `EmbeddingDimensionError` when the two disagree, which turns a
  silent mismatch into a readable failure.
- **Outcome**: Single documented dimension of 384.

- **Date**: 2026-08-27
- **Decision**: Write a ~90-line `Vector` SQLAlchemy `UserDefinedType`
  (`app/embeddings/vector_type.py`) instead of adding the `pgvector`
  Python package.
- **Problem**: The column needs DDL that emits `vector(384)` and a value
  conversion asyncpg accepts — asyncpg has no codec for an extension
  type, so the value travels in pgvector's text form with a CAST on the
  way in.
- **Alternatives**: Add `pgvector>=0.5` (resolves clean, zero transitive
  dependencies) and use `pgvector.sqlalchemy.Vector`.
- **Trade-off**: The package's real value is its distance operators
  (`<->`, `<=>`, `<#>`), and this slice is explicitly forbidden from
  computing similarity or querying nearest neighbours — so adopting it
  now would be a dependency for an API we may not call (CLAUDE.md rule
  6). The cost is ~90 lines we own, including a float32 rounding detail
  that the package would have handled. Swapping to it later is an import
  change plus a no-op migration; the database-side column is identical
  either way.
- **Outcome**: Custom type for now; adopt `pgvector` in the slice that
  actually runs a similarity query.

- **Date**: 2026-08-27
- **Decision**: Put `user_id` inside the embedding uniqueness key —
  `UNIQUE(user_id, source_type, source_id, chunk_index,
  model_identifier)` — rather than deriving ownership through a join
  the way `skill_evidence` does.
- **Problem**: `source_type`/`source_id` is a polymorphic string pair
  with no foreign key behind it, so there is no single join that proves
  ownership across all three source kinds. Ownership enforced only in
  application code is a check someone eventually forgets to write.
- **Alternatives**: (1) no `user_id` column, reaching the owner through
  each source table (the `skill_evidence` pattern); (2) a `user_id`
  column outside the unique key, filtered on by convention.
- **Trade-off**: A denormalized owner can in principle drift from its
  source row's owner — the exact risk `skill_evidence` avoids by not
  storing one. Accepted because there is no join to drift *against* for
  a polymorphic key, and because it buys a structural guarantee: no read
  path in `app/embeddings/store.py` can address a row without naming
  whose it is, and two users referencing the same `source_id` get
  separate rows instead of silently sharing one.
- **Outcome**: `user_id` is NOT NULL and part of the natural key.

- **Date**: 2026-08-27
- **Decision**: Store only `content_hash` for embedded text — no source
  text column on `embeddings` — and explain a chunk with
  `char_start`/`char_end` offsets into the source row instead.
- **Problem**: Semantic retrieval later needs to show *where* a chunk
  came from, which normally argues for keeping the chunk text beside the
  vector.
- **Alternatives**: Store the chunk text alongside the vector.
- **Trade-off**: Reading a chunk back now costs a join to its source
  row, and for `skill_evidence`-derived documents there is no contiguous
  span at all, so the offsets are NULL there. In exchange, no second
  copy of private resume prose exists on a different access path, and a
  chunk is provably the same characters the source still holds.
- **Outcome**: Hash plus offsets; `app/embeddings/content.py` guarantees
  `chunk.text == source[char_start:char_end]`.

- **Date**: 2026-08-27
- **Decision**: Add `public` after `careerlens_test` on the test
  suite's `search_path` (`apps/api/tests/conftest.py`).
- **Problem**: A PostgreSQL extension belongs to exactly one schema per
  database, and pgvector is installed into `public` by both
  `infra/postgres/init.sql` and migration `5b0b21f962b1`. With the test
  search path set to the isolated schema alone, creating a table with a
  `vector` column fails with `type "vector" does not exist`.
- **Alternatives**: (1) schema-qualify the type as `public.vector` in
  `app/embeddings/vector_type.py`; (2) install a second copy of the
  extension into the test schema (not possible — one instance per
  database).
- **Trade-off**: Slightly weaker isolation in principle: a name absent
  from `careerlens_test` now falls through to `public` instead of
  failing. In practice every product table is created in the test schema
  by `Base.metadata.create_all` and the test schema is searched first, so
  the only name that actually falls through is the extension's type.
  Option (1) was rejected because it hard-codes a schema into
  application code that a differently-provisioned database would break.
- **Outcome**: `search_path = careerlens_test,public`, matching how a
  real deployment is arranged (extensions in `public`, on the path).

- **Date**: 2026-08-22
- **Decision**: Isolate `apps/api` tests with a dedicated Postgres *schema*
  (`careerlens_test`), created/dropped by the test suite itself, rather than
  a separate physical database.
- **Problem**: Prompt 0.3 adds DB-backed tests but defines no product tables
  yet, so there's no data to isolate today — the goal is establishing a
  pattern that won't need rework once tables exist.
- **Alternatives**: (1) a second physical database, provisioned via
  `infra/postgres/init.sql` or a CI/local setup step; (2) no isolation at
  all — run tests directly against the dev database's default schema.
- **Trade-off**: A schema is simpler than a second database (no
  `docker-compose.yml`/init-script changes, no extra CI setup) and is fully
  self-managed by the test suite, but it shares the same Postgres role/
  connection limits as dev, and doesn't isolate server-level settings.
- **Outcome**: Went with the schema approach (`apps/api/tests/conftest.py`).

- **Date**: 2026-08-22
- **Decision**: Enable the `pgvector` extension via an Alembic migration,
  even though `infra/postgres/init.sql` already does the same thing at
  container first-boot.
- **Problem**: `init.sql` only runs once, on a fresh Docker volume — it
  doesn't help a CI database, a manually-created Postgres instance, or a
  future staging/production database that wasn't bootstrapped from our
  compose file.
- **Alternatives**: Rely on `init.sql` alone; skip the migration.
- **Trade-off**: Slight duplication (both statements are the same idempotent
  `CREATE EXTENSION IF NOT EXISTS vector`), but the migration is what makes
  the extension reproducible via `alembic upgrade head` on *any* Postgres
  instance, not just ones using our compose file.
- **Outcome**: Kept both — `init.sql` for convenient local container
  first-boot, the migration as the portable, authoritative schema history.

- **Date**: 2026-08-22
- **Decision**: Verify "the web page can reach the API" in `scripts/smoke.sh`
  via `curl` (checking the API's CORS response header and the web page's
  HTML markup), instead of a headless browser.
- **Problem**: The web page's health check runs client-side (`"use client"`
  + `useEffect`), so no plain HTTP request can observe the *resolved*
  "API is healthy" text the way a real browser would after executing
  JavaScript.
- **Alternatives**: Add Playwright/Puppeteer and actually render the page.
- **Trade-off**: A headless browser would give a direct, literal answer,
  but is a new, fairly heavy dependency for a "minimal" smoke test at this
  stage. The `curl`-based check instead verifies the exact mechanism that
  would make or break the page's fetch in a real browser (CORS) plus that
  the expected component actually renders into the served HTML — a real,
  specific check, just not a literal one. The gap (JS execution) is covered
  by a documented manual step instead.
- **Outcome**: `curl`-based CORS + markup checks, zero new dependencies;
  manual browser confirmation documented in `README.md` as the final step.

- **Date**: 2026-08-22
- **Decision**: Store refresh tokens as opaque random strings hashed with
  SHA-256, not as JWTs.
- **Problem**: A refresh token needs to be revocable server-side (logout,
  rotation, compromise) — a pure stateless JWT can't be "un-issued"
  without a separate blocklist anyway, so there's little benefit to
  making it self-describing.
- **Alternatives**: Issue refresh tokens as JWTs too (with a `type:
  refresh` claim); store the raw refresh token value directly instead of
  a hash.
- **Trade-off**: An opaque token needs a database row per session (one
  extra table, one extra query on refresh), but that row is exactly what
  makes rotation and revocation simple and immediate, rather than needing
  a growing blocklist. Storing a hash instead of the raw value means a
  database leak alone can't be used to impersonate anyone — the same
  reasoning as password hashing, just with a fast hash (SHA-256) instead
  of a slow one (bcrypt), since the input is already 256 bits of random
  data, not a guessable human password.
- **Outcome**: `refresh_tokens.token_hash` (SHA-256), rotation on every
  use, and reuse-of-a-revoked-token cascades to revoke the whole session.

- **Date**: 2026-08-22
- **Decision**: In-memory rate limiting for `/auth/register`, `/login`,
  `/refresh` (10 requests/60s per client IP, shared budget), rather than
  a Redis-backed limiter.
- **Problem**: These endpoints need *some* protection against
  credential-guessing and account-creation abuse from day one.
- **Alternatives**: Redis-backed counters (Redis is already provisioned
  via docker-compose, just unused by the API so far).
- **Trade-off**: In-memory counters are single-process — they reset on
  restart and are not shared across multiple API instances, so this
  would under-count (and under-protect) in a scaled, multi-instance
  deployment. Redis would fix that but adds a new client dependency and
  makes the auth test suite depend on Redis being reachable too, for a
  "strategy" that doesn't need to be the final production answer yet.
- **Outcome**: In-memory (`app/rate_limit.py`), explicitly documented as
  single-process-only; swapping in a Redis-backed limiter is a natural,
  isolated follow-up once there's a multi-instance deployment to protect.

- **Date**: 2026-08-22
- **Decision**: Login returns an identical, generic error for both "no
  such account" and "wrong password"; registration returns a specific
  `409` for a duplicate email.
- **Problem**: Login is a credential-guessing surface — a specific
  "no such account" response lets an attacker enumerate which emails
  have accounts before ever guessing a password. Registration is a
  different situation: a client that submits an email deliberately wants
  an account tied to it, and needs an actionable reason if it can't have
  one.
- **Alternatives**: Generic errors everywhere, including registration
  (silently "succeeding" or giving a vague error on a duplicate email);
  specific errors everywhere, including login.
- **Trade-off**: A fully generic registration response is a worse
  experience for a very small enumeration-risk reduction — duplicate-email
  detection on signup is normal, expected behavior in virtually every
  real product, unlike revealing account existence through login.
- **Outcome**: Generic `401` (`"invalid email or password"`) for login
  failures of any cause; specific `409` (`"email already registered"`)
  for registration only.

- **Date**: 2026-08-22
- **Decision**: For the browser flow, move the refresh token out of the
  JSON response entirely and into an `HttpOnly`/`Secure`/`SameSite=Lax`
  cookie scoped to `/api/v1/auth`; the access token stays in-memory only
  in the frontend (no `localStorage`, no cookie).
- **Problem**: Prompt 1.1's API returned the raw refresh token in the
  login/refresh JSON body. Any JavaScript on the page — including an
  XSS payload — could read that response and steal a long-lived
  credential. An `HttpOnly` cookie is invisible to JavaScript by design;
  keeping the token in the JSON response too would have defeated that
  protection for the one client (the browser) it matters most for.
- **Alternatives**: Keep returning the refresh token in JSON and let the
  browser store it (localStorage or an app-managed cookie); accept it
  from either a cookie or a JSON body on `/refresh`/`/logout` for
  backward compatibility with non-browser clients.
- **Trade-off**: A cookie-only design means the API now depends on
  `allow_credentials=True` CORS and a same-site deployment (or, later,
  matching cookie `Domain` values across subdomains) to work at all —
  more coupling between web and API than a bearer-token-everywhere
  design would have. Accepted deliberately: the security property (raw
  token never JS-readable) is worth more than that flexibility for this
  product's browser-first flow. Non-browser clients aren't a real use
  case yet.
- **Outcome**: `AccessTokenResponse` (Prompt 1.1's `TokenPairResponse`,
  minus `refresh_token`); `/refresh` and `/logout` read the cookie only,
  no JSON-body alternative — verified live (browser `document.cookie`
  returns `""`, response body confirmed to omit the field) and by
  `test_login_response_never_includes_the_raw_refresh_token` and
  `test_login_sets_an_httponly_secure_samesite_cookie_scoped_to_auth`.

- **Date**: 2026-08-22
- **Decision**: Deduplicate concurrent calls to the frontend's
  `refresh()` into a single in-flight request (`api-client.ts`).
- **Problem**: Found live, not in a mocked test: React's Strict Mode
  double-invokes effects in development, so `AuthProvider`'s mount-time
  silent-refresh fired twice almost simultaneously. The API rotates the
  refresh token on every use and revokes the *entire* session if an
  already-used token is presented again (Prompt 1.1's reuse-detection) —
  so the second, losing call presented the first call's now-rotated-away
  cookie, which the server correctly treated as a reuse/compromise
  signal and revoked the session the first call had just established.
  Not React-specific in principle: two browser tabs mounting at once
  could hit the same race.
- **Alternatives**: Weaken server-side reuse detection with a grace
  period for near-simultaneous reuse (used by some real-world rotating-
  refresh-token implementations); guard only the React effect itself
  (e.g. a mount ref) rather than the API client.
- **Trade-off**: Fixing it in the API client protects every caller
  (present and future), not just this one effect, and keeps the
  server's reuse detection maximally strict — the safer place to relax
  a security control is nowhere, if the client can simply not create
  the race in the first place.
- **Outcome**: `refresh()` caches its in-flight promise; concurrent
  callers share the one request. Covered by
  `api-client.test.ts` (`refresh > deduplicates concurrent calls...`).

- **Date**: 2026-08-22
- **Decision**: Tolerate reuse of a revoked refresh token — without
  cascading revocation — only when it is *structurally* the immediate,
  still-active predecessor of the current token (via a new
  `replaced_by_id` link) *and* was revoked within a short configurable
  window (`AUTH_REFRESH_REUSE_GRACE_SECONDS`, default 5s). Rotation now
  uses `SELECT ... FOR UPDATE` to serialize concurrent attempts on the
  same token row.
- **Problem**: The client-side fix above (deduplicating same-page
  concurrent calls) doesn't help across two independent JS contexts —
  two rapid page reloads, or two browser tabs, each still holding the
  same pre-rotation cookie when they fire their own `/refresh`. Live
  reproduction confirmed: the losing request's failure was cascading
  the whole session, including the token the winning request had just
  issued. Proven live and via `test_concurrent_refresh_with_same_token_
  one_wins_one_loses_and_session_survives`.
- **Alternatives considered**: (1) A pure time-based grace period with
  no structural check — tolerates *any* recently-revoked token, which
  in a rapid-reload loop could accidentally tolerate a token several
  generations old. (2) Structural adjacency with no time bound —
  doesn't decay: a long-idle session's one-hop-back predecessor would
  stay tolerable indefinitely, a wider window than a short grace period
  gives. (3) Transparently handing the race-loser the winner's live
  token pair instead of a 401 — fully seamless UX, but adds a new path
  that returns real credentials from what's structurally an error
  condition; rejected in favor of the smaller, lower-risk change.
- **Trade-off**: `AUTH_REFRESH_REUSE_GRACE_SECONDS` trades security for
  UX in one narrow, bounded way — a stolen token that is *both* the
  exact immediate predecessor *and* replayed within this window is
  tolerated as if benign. Kept deliberately short (default 5s): ample
  for realistic browser/network race timing, not for an attacker who
  wasn't already racing the legitimate client in real time. Anything
  structurally older, or replayed later, is unaffected — full cascade,
  exactly as before.
- **Outcome**: `refresh_tokens.replaced_by_id` (migration
  `6826a2b31fcd`), `with_for_update()` on rotation, benign-race check in
  `app/api/v1/auth.py`. No frontend change — the API contract (request/
  response shape) is unchanged. Regression tests cover the race
  (concurrent + sequential), multi-generation reuse, and reuse after the
  grace window, each still triggering the original cascade behavior.

- **Date**: 2026-08-23
- **Decision**: For the candidate profile (Prompt 1.3): a `profiles`
  table keyed 1:1 on `users.id` (its own `user_id` primary key, no
  separate surrogate id) for scalar fields; a canonical, deduplicated
  `skills` lookup table (`name` + a lowercased `slug` unique key) linked
  through a `profile_target_skills` join table for declared target
  skills; and a plain normalized `profile_target_roles` text table (no
  lookup table) for target roles. Endpoints are id-addressable —
  `GET`/`PATCH /api/v1/profiles/{user_id}` — with an explicit ownership
  check (`user_id != current_user.id` → 403) as the first line of both
  handlers, rather than an implicit self-only `/profile`.
- **Problem**: Six fields to store, two of them multi-valued
  (target roles, target skills), with no existing product tables to
  extend. Needed a normalization approach for the multi-valued fields,
  and an endpoint shape that actually demonstrates and tests ownership
  enforcement rather than making cross-user access structurally
  unreachable.
- **Alternatives**: (1) Comma-separated `target_roles`/`target_skills`
  text columns on `profiles` directly — violates 1NF, turns "does this
  profile want skill X" into string matching instead of a join, and
  can't dedupe/canonicalize. (2) A `roles` lookup table mirroring
  `skills`, for symmetry. (3) A self-only `/api/v1/profile` endpoint
  (no id in the path, always scoped to the caller), matching the
  existing `/api/v1/auth/me` idiom.
- **Trade-off**: Skills get a lookup table because Prompt 2.x's
  resume-extracted skills and later job-required skills are expected to
  match against this exact same canonical identity for evidence-first
  scoring (project brief's core rule) — "Python" and "python" must
  resolve to one row across the whole system, not just within one
  profile. Target roles have no known cross-entity matching need yet —
  they're a declared preference, not a scored signal — so a `roles`
  lookup table would be speculative abstraction; a plain normalized
  text table (still 1NF: one row per role, no comma-separated list)
  covers today's requirement without it. The id-addressable endpoint
  shape means ownership is a runtime-checked property with a real
  403 test, not a structural guarantee — marginally more code (one
  explicit comparison) than a self-only route, but it is what makes
  "ownership-enforced endpoints" and "a cross-user access failure test"
  (as asked for) meaningful rather than vacuous.
- **Outcome**: `profiles`, `skills`, `profile_target_roles`,
  `profile_target_skills` (migration `8156d76f48cc`). `PATCH` uses
  Pydantic's `exclude_unset` for partial updates; an omitted field means
  "unchanged", explicit `null` clears an optional scalar, and
  `target_roles`/`target_skills` fully replace the existing set when
  present. Covered by `tests/test_profile.py`, including
  `test_cannot_get_another_users_profile` and
  `test_cannot_patch_another_users_profile`.

- **Date**: 2026-08-23
- **Decision**: Add `"PATCH"` to `CORSMiddleware`'s `allow_methods` in
  `apps/api/app/main.py`.
- **Problem**: Found live via the browser: Prompt 1.3's profile update
  endpoint uses `PATCH /api/v1/profiles/{user_id}`, but
  `allow_methods` was still `["GET", "POST"]` from Prompt 1.1 (auth-only
  at the time). Starlette's `CORSMiddleware` rejects a preflight whose
  `Access-Control-Request-Method` isn't in `allow_methods` with a plain
  400 *before* the request reaches routing — so every "Save profile"
  click failed at the preflight, the real `PATCH` was never sent, and
  the route/ownership/validation code (already covered by
  `tests/test_profile.py`) was never at fault or even reached.
- **Alternatives**: `allow_methods=["*"]` (wildcard everything the app
  will ever need, present or future); route around CORS by proxying
  `/api` through the Next.js dev server instead of calling `localhost:8000`
  directly.
- **Trade-off**: A wildcard never has this class of bug again, but also
  silently allows any future method (e.g. `DELETE`) without a deliberate
  decision to expose it — listing methods explicitly means adding one is
  a visible, one-line change each time, matching how `POST` was added
  explicitly in Prompt 1.1. Proxying through Next.js would remove the
  cross-origin request (and this whole bug class) entirely, but is a
  bigger architectural change than this fix warrants right now.
- **Outcome**: `allow_methods=["GET", "POST", "PATCH"]`. Verified live
  (preflight now returns `200` with `PATCH` in
  `Access-Control-Allow-Methods`; a real browser "Save profile" now
  completes and persists across reload) and by
  `tests/test_cors.py` (`test_preflight_allows_patch_on_the_profile_endpoint`,
  plus non-regression/negative coverage for the existing auth `POST`
  preflight and an unconfigured origin).

- **Date**: 2026-08-23
- **Decision**: For resume upload (Prompt 2.1): a `resumes` table with
  its own `id` (unlike `profiles`, addressed by the owner's `user_id`,
  a resume is 1-of-many so it needs its own identity); a small
  `ResumeStorage` interface (`save`/`read`/`delete`, `app/storage/`)
  with a `LocalResumeStorage` implementation writing under a configured
  directory, keyed by an opaque, server-generated `storage_key`
  (`{user_id}/{resume_id}.{ext}`) that is never returned in any API
  response; and reject-before-persist validation — extension,
  declared-Content-Type-matches-extension, real file-signature ("magic
  bytes") check, size, and filename safety all run before anything is
  written to the database or disk, so a rejected upload leaves no row
  and no file behind.
- **Problem**: A file upload endpoint needs to (a) never trust a
  client's filename or Content-Type as ground truth for what a file
  actually is, (b) never leak where a file physically lives, since a
  path is both an internal implementation detail and, for local
  storage, a potential traversal/access surface, and (c) not hard-wire
  "local disk" into the resume domain code, since project-brief calls
  for S3-compatible storage in production.
- **Alternatives**: (1) Trust the browser's declared `Content-Type` and
  the filename extension alone — rejected: both are client-supplied and
  trivially spoofable (renaming `evil.exe` to `resume.pdf` satisfies
  both checks; only reading the file's actual first bytes catches it).
  (2) Store the file at a path derived from the original filename —
  rejected: makes path-traversal a live concern (`../../etc/passwd`)
  and collisions likely (two users, or two uploads, both naming a file
  `resume.pdf`). (3) Call `open()`/`Path.write_bytes()` directly from
  the route instead of a storage interface — simpler today, but bakes
  "local filesystem" into the route/domain code that Prompt 7.x's S3
  migration would then have to rewrite instead of swap.
- **Trade-off**: The storage interface is a small amount of extra
  indirection (a Protocol, one implementation, one dependency-injected
  factory) for code that only has one backend today — justified because
  the project brief explicitly commits to a second (S3-compatible)
  backend later, and FastAPI's `Depends()` makes the indirection nearly
  free (tests override it exactly like `get_db`, pointing at a temp
  directory instead of a real one). Reject-before-persist is simpler
  than a "store with a failed status" model, at the cost of not
  keeping any record that a rejected upload was ever attempted — judged
  fine, since an invalid upload was never really "the user's resume" to
  begin with.
- **Outcome**: `resumes` table (migration `5c4a2b275fdb`),
  `app/storage/{base,local}.py`, `POST`/`GET`/`GET
  {id}`/`DELETE {id}` under `/api/v1/resumes`, all requiring
  `get_current_user` and (for the two id-addressed routes) an explicit
  `resume.user_id == current_user.id` check — 404 if the id doesn't
  exist, 403 if it exists but isn't the caller's. `allow_methods` in
  `app/main.py` gained `"DELETE"` up front this time, applying the
  lesson from the Prompt 1.3 CORS bug before it could repeat. Covered
  by `tests/test_resume.py` (including
  `test_cannot_get_another_users_resume` and
  `test_cannot_delete_another_users_resume`) and
  `tests/test_local_storage.py`; verified live via a real browser
  upload → list → delete cycle with fictional sample PDF/DOCX files.

- **Date**: 2026-08-23
- **Decision**: Remove the explicit `router.push("/login")` from
  `DashboardPage`'s `handleLogout` (`apps/web/src/app/dashboard/page.tsx`);
  the existing `useEffect` that redirects whenever `status ===
  "unauthenticated"` is now the sole place that navigates to `/login`.
- **Problem**: Found live in the real browser (not caught by the
  existing mocked-router unit tests): logging out fired **two**
  separate client-side navigations to `/login` — one from
  `handleLogout`'s own `router.push`, one from the protective effect
  reacting to `logout()` setting `status` to `"unauthenticated"`.
  Confirmed at the network level (two back-to-back, identical `GET
  /login?_rsc=...` requests per logout) on every repro attempt. Two
  overlapping App Router client transitions to the same href is exactly
  the kind of race that can leave the router's pending-navigation state
  stuck — reported symptom: the page visually stuck on the dev
  "Rendering…" indicator and non-interactive until a hard refresh,
  intermittent because it depends on how much the two transitions
  overlap (worse under dev-mode HMR/compile latency).
- **Alternatives**: (1) Guard against the double push with a ref/flag
  (e.g. `hasNavigatedRef`) — works, but treats the symptom rather than
  the cause, and leaves two competing "authorities" deciding when to
  redirect. (2) Keep the explicit push in `handleLogout` and remove the
  effect instead — rejected: the effect is also what protects
  `/dashboard` when a mount-time silent refresh finds no session at
  all, a case `handleLogout` never covers.
- **Trade-off**: None of substance — the effect already unconditionally
  handles every case that causes `status` to become `"unauthenticated"`,
  logout included, so removing the redundant call is strictly a
  simplification, not a behavior change from the user's perspective.
- **Outcome**: One redirect authority. Verified live, twice: exactly one
  `GET /login?_rsc=...` per logout, page interactive immediately after
  in both runs. `tests/dashboard/page.test.tsx`'s logout test
  strengthened to assert `pushMock` was called `toHaveBeenCalledTimes(1)`
  — confirmed this fails (`2 times`) against the old code and passes
  against the fix. Noted gap: every navigation test in this repo mocks
  `next/navigation`'s `useRouter` entirely, so no unit test exercises
  the real App Router transition machinery — this class of bug can only
  be caught by a call-count assertion (as added) or live browser
  verification, not by the mock alone.

- **Date**: 2026-08-23
- **Decision**: Add a `supersededRef` to `AuthProvider` (`auth-context.tsx`):
  set to `true` the instant an explicit `login()` starts, checked by the
  mount-time silent-refresh effect's `.then`/`.catch` before either
  applies its result. Once set, it's never reset.
- **Problem**: Found live (not by any existing test — this repo had no
  `auth-context` test at all): the mount-time `refresh()` effect and a
  manual `login()` are two independent flows that both write
  `status`/`user`/`accessToken` with no sequencing. If `refresh()`
  (started on page load, restoring a session from the HttpOnly cookie)
  was still pending or hadn't even fired yet when the user submitted the
  login form, and it settled *after* login already succeeded, its
  unconditional `setStatus("unauthenticated")` silently reverted a real,
  more recent session — no error, no console warning, just a reverted
  `status` that then bounced `/dashboard`'s protective effect back to
  `/login`, leaving the "Signing in…" button and the router's pending
  transition both stuck. Directly proved live by patching `fetch` to
  delay `/auth/refresh` and confirming `login()` could complete (200,
  200, `/dashboard` rendered) while the delayed `refresh()` was still
  unsettled — i.e., two independent writers to the same state, live,
  not just in theory.
- **Alternatives**: (1) A generation/version counter incremented on every
  auth-changing operation, checked by every writer — more general, but
  overkill for the one actual conflict (mount-refresh vs. login);
  nothing else currently races. (2) Cancel the mount effect's promise
  chain outright once `login()` starts (e.g. via `AbortController`) —
  more "correct" in the sense of not doing unnecessary work, but the
  network request has typically already been sent by the time a user
  can react, so aborting saves nothing and adds a fetch-abort-handling
  path for no behavioral difference from just ignoring the result.
- **Trade-off**: The ref never resets after first use, which is safe
  only because the mount effect's promise settles exactly once per
  `AuthProvider` mount (verified across: initial refresh, explicit
  login, logout, a later login on the same still-mounted provider, and
  a full page reload/remount, which creates a fresh ref via a fresh
  component instance) — a design that would break if the mount effect
  were ever changed to run more than once per mount.
- **Outcome**: `apps/web/src/lib/auth-context.tsx`. New
  `apps/web/src/lib/auth-context.test.tsx`, using a manually-sequenced
  promise (same technique as `api-client.test.ts`'s `refresh()` dedup
  test) to deterministically prove the fix — confirmed to fail against
  the old code (`status: unauthenticated`) and pass against the fix.
  Live-verified twice more: a normal login, and the same delayed-refresh
  race that previously exposed the bug, both ending on `/dashboard`
  without reverting.

- **Date**: 2026-08-23
- **Decision**: For resume text extraction (Prompt 2.2): Celery
  (broker: the already-provisioned Redis, no result backend — Postgres,
  the `resumes` row itself, is the only source of truth for job state),
  `pypdf`/`python-docx` for deterministic extraction, and `resumes.status`
  renamed `uploaded/processing/completed/failed` ->
  `queued/processing/succeeded/failed` to match this prompt's own
  wording. Duplicate/concurrent processing is prevented by an atomic
  claim (`UPDATE resumes SET status='processing' WHERE status='queued'`)
  rather than any queue-level dedup feature; retries distinguish
  transient failures (a storage read error — worth retrying) from
  permanent ones (a parse failure, or no extractable text — never
  worth retrying, since the same bytes fail the same way every time).
- **Problem**: Needed an async worker for a slow, request-blocking
  operation (parsing an uploaded file), using infrastructure this
  project already provisions, with job state a client can poll for,
  safe (non-leaking) error messages, and correctness under duplicate/
  concurrent triggering.
- **Alternatives**: (1) `arq` (an asyncio-native Redis queue) instead
  of Celery — would have avoided the sync/async bridge entirely
  (`asyncio.run(...)` inside a sync Celery task calling back into async
  SQLAlchemy), and was seriously considered — but `docs/project-brief.md`
  names Celery specifically ("Redis and Celery only for processing that
  should not block a request"), not just "a queue", so this follows
  that rather than substituting a technically-tidier personal
  preference. (2) A Celery result backend (also Redis) as the source of
  truth for job state instead of a Postgres column — rejected: it would
  create two places extraction state could live and disagree, for no
  benefit here (nothing ever calls `.get()` on a Celery `AsyncResult`).
  (3) arq's built-in `_job_id` dedup for duplicate prevention — not
  applicable to Celery, and the atomic claim guards the thing that
  actually matters (concurrent *execution*) regardless of what caused a
  duplicate trigger, not just duplicate enqueueing.
- **Trade-off**: Celery's task model is synchronous; bridging into
  async SQLAlchemy/`ResumeStorage` per task execution (via
  `asyncio.run`) is the accepted cost of using the framework the brief
  specifies rather than the one that would have fit this codebase's
  existing async-everywhere style most naturally. Internal retry state
  (`attempt_count`) mirrors Celery's own `self.request.retries` rather
  than being hand-maintained, keeping "how many times has this run" a
  single source of truth. `task_eager_propagates` is deliberately left
  off in tests — enabling it makes Celery's own internal retry
  (`self.retry()`) propagate as an exception instead of being handled,
  discovered by a real, reproducible test failure, not by reading docs
  closely enough the first time.
- **Outcome**: `apps/api/app/worker.py` (Celery app, task, atomic
  claim, `enqueue_extraction`), `apps/api/app/extraction.py` (pure
  pypdf/python-docx functions), new `resumes` columns
  (`extracted_text`, `error_message`, `attempt_count`, `processed_at`;
  migration `266984262a64`, which also data-migrates existing rows'
  `status` values to the new vocabulary). `GET /api/v1/resumes` and
  `GET /api/v1/resumes/{id}` (Prompt 2.1) serve as the status endpoint
  as-is — no new route. Dashboard polls every 2s while any resume is
  queued/processing. Covered by `tests/test_extraction.py` (successful
  PDF/DOCX, malformed document, transient-failure retry-then-succeed,
  retries-exhausted safe failure, duplicate-processing prevention,
  idempotent re-run) via Celery's `task_always_eager` mode — no real
  broker/worker/Redis connection needed for any of it.

- **Date**: 2026-08-23
- **Decision**: Reconcile the resumes that Prompt 2.2's migration left
  stranded in `queued` with a one-off, manually-run backfill script
  (`apps/api/scripts/requeue_stuck_resumes.py`, `make
  requeue-stuck-resumes`) rather than any automatic mechanism. The
  migration (`266984262a64`) renames existing Prompt 2.1 rows
  `uploaded -> queued` so legacy and new rows share one vocabulary and
  a never-processed resume looks the same however it got there — but
  that rename is pure SQL and deliberately does *not* enqueue Celery
  jobs: a migration must stay applicable without Redis/Celery being
  reachable (CI, a fresh environment, or any `alembic upgrade head` run
  before a broker exists), and coupling schema changes to a live broker
  would make migrations fail for reasons that have nothing to do with
  the schema. The consequence is a real gap — those rows have no Celery
  message and `enqueue_extraction` is only ever called from the upload
  endpoint — which the script closes by querying `status = 'queued'`
  and calling that same existing `enqueue_extraction`, duplicating no
  task logic.
- **Problem**: Legacy Prompt 2.1 resumes became `queued` with nothing
  to process them, and would have stayed stuck there permanently: there
  is no periodic sweep, no worker-startup hook, and no manual retry
  endpoint.
- **Alternatives**: (1) Enqueue from inside the migration — rejected
  for the broker-coupling reason above. (2) A `worker_ready` signal
  sweep on Celery worker startup — genuinely considered, and it would
  also self-heal the separate best-effort-enqueue gap, but it adds
  permanent runtime behavior (and, per Celery's `Consumer.start()`
  reconnect loop, re-fires on every broker reconnect, not just once per
  process) to fix what is a one-time historical data gap. (3) Celery
  Beat periodic reconciliation — a whole extra process to run and
  supervise, far past what this gap warrants.
- **Trade-off**: The fix is manual, so it only helps if someone
  actually runs it after migrating — accepted, because the gap is
  one-time and this project is local-only. The still-open, separately
  documented gap is that `enqueue_extraction` is best-effort at upload
  time (a Redis outage silently leaves a new resume `queued`); this
  script also fixes those rows when run, but nothing runs it
  automatically, so that remains a known limitation rather than a
  solved problem. The script is a sync entry point doing async DB work
  via `asyncio.run`, mirroring `app/worker.py`'s own split — enqueueing
  is synchronous broker I/O and belongs outside the event loop (this
  also happens to be what makes it testable under `task_always_eager`,
  where an enqueue inside a running loop would hit "asyncio.run()
  cannot be called from a running event loop").
- **Outcome**: The script is safe to rerun by construction, and needs
  no dedup logic of its own: it is read-only against `resumes` (it
  never changes status — the only transition out of `queued` stays the
  worker's atomic `UPDATE ... WHERE status = 'queued'` claim in
  `_claim_resume`), and its `status = 'queued'` filter already skips
  anything processing or finished. If the same resume is enqueued twice
  anyway — a rerun racing a still-unconsumed message, or two operators
  — Postgres serializes the two claim UPDATEs and only the first
  matches a row; the second no-ops without writing. So a duplicate
  costs one wasted task execution, never double processing. Covered by
  `tests/test_requeue_stuck_resumes.py` (migrated PDF/DOCX row
  re-enqueued and processed, rerun is a no-op, redundant in-flight
  enqueue is harmless, processing/succeeded/failed rows skipped,
  mixed-set filtering, and malformed/transient-retry/retries-exhausted
  behavior all still intact through this path).

- **Date**: 2026-08-23
- **Decision**: For the skill taxonomy and evidence schema (Prompt 2.3):
  EXTEND the existing `skills` table (Prompt 1.3, migration
  `8156d76f48cc`) with a nullable `category` plus new `skill_aliases`
  and `skill_relations` tables — rather than adding a second "canonical
  skills" table — and add `candidate_skills` (one row per
  candidate/skill, `UNIQUE(user_id, skill_id)`) and `skill_evidence`
  (hanging off `candidate_skills`). Aliases are a normalized table with
  a **globally** unique `alias_slug`; relations are an untyped,
  self-referential M:N with both directions materialized. The taxonomy
  is seeded by a repeatable command (`make seed-skills`) from a typed
  Python file, never by the migration.
- **Problem**: Needed somewhere to record which candidate has which
  skill and, per docs/project-brief.md's Evidence-First rule, why we
  believe it — with enough structure for Prompt 2.4's deterministic
  extraction to write into, without pre-building scoring or matching.
- **Alternatives**: (1) A new `canonical_skills` table alongside
  `skills` — rejected outright: `skills` is already the canonical
  vocabulary and is already referenced by `profile_target_skills`, so a
  second table would fork skill identity and break this file's own
  Prompt 1.3 commitment that "Python" and "python" resolve to one row
  across the whole system. Extending also means a skill a user already
  coined gets *adopted* by the seed rather than duplicated. (2) Aliases
  as a JSON/array column on `skills` — rejected: the entire purpose of
  an alias is indexed reverse lookup ("js" -> JavaScript), and JSON
  cannot enforce the guarantee that actually matters, that one alias
  resolves to exactly one skill. (3) A `relation_type` column on
  relations (prerequisite/parent/substitute) — deliberately omitted;
  see the trade-off below. (4) Denormalizing `user_id` onto
  `skill_evidence` — rejected: a copy can drift from its parent, and
  the join back through `candidate_skills` is cheap.
- **Trade-off**: **Relations are untyped and mean only "these commonly
  appear together".** They must never be read as prerequisite,
  parent/child, substitute, dependency, or hierarchy — Python <->
  Django asserts association, not "Django requires Python". Encoding
  direction would need a typed vocabulary and is an additive migration
  when a feature actually needs it. **Evidence's natural key is
  `(candidate_skill_id, source_type, source_identifier,
  extraction_method)`**, so there is one representative excerpt per
  candidate-skill/source/method: five mentions of "Python" in one
  resume produce one evidence row, not five. That is sufficient for the
  MVP because the excerpt exists to let a human verify the claim, and
  one clear quotation does that as well as five near-identical ones,
  while every question the MVP asks ("which sources support this?",
  "how confident?", "show me why") is answered per source rather than
  per mention; counting mentions is scoring, which belongs to Prompt
  4.x. Wanting multiple excerpts per source later is a contained,
  additive change: add an `excerpt_hash` (or `occurrence_index`) column
  and extend that unique constraint to include it — no change to
  ownership, cascades, or any other table. `source_identifier` is a
  polymorphic string, not an FK (no single FK can span resume/github/
  manual), so deleting a resume leaves evidence citing an id that no
  longer resolves — accepted for now, since nothing in 2.3 writes
  evidence. `source_type`/`extraction_method` get no database CHECK,
  matching `resumes.status`'s reasoning that a vocabulary should grow
  without a migration; `confidence` DOES get one, because a numeric
  range is a permanent invariant rather than a vocabulary.
- **Outcome**: Migration `610680fe7d6a` (schema only — no rows, for the
  same reason `266984262a64` did not enqueue Celery jobs). Taxonomy is
  33 skills across 7 categories in `app/seeds/skill_taxonomy.py`, a
  typed Python module rather than JSON so it is mypy-checked and can
  carry the alias-quality rule in comments: an alias must be an
  unambiguous technical identifier, never a common English word —
  "next", "rest" and "node" were rejected on that basis to avoid false
  matches in Prompt 2.4. `scripts/seed_skills.py` validates the file
  (no duplicate skills, no alias shadowing a canonical slug, no alias
  owned by two skills, no self- or dangling relations) before touching
  the database, then upserts by slug and reconciles aliases/relations
  by difference in one transaction — so a re-run writes nothing at all
  (verified: `updated_at <> created_at` matches zero rows after three
  runs) and skills absent from the file are never touched. Covered by
  `tests/test_skill_taxonomy.py` (constraints, cascades, RESTRICT,
  confidence bounds, multi-source evidence) and
  `tests/test_seed_skills.py` (idempotency asserted on ids and
  timestamps, adoption of a user-coined row, off-taxonomy survival,
  repair of hand-edits, alias/relation reconciliation, and an
  integration test proving a profile `PATCH` of "python" reuses the
  seeded row instead of coining a second).

- **Date**: 2026-08-24
- **Decision**: For deterministic resume skill extraction (Prompt 2.4):
  compile the curated taxonomy's canonical names and aliases into
  boundary-guarded, separator-tolerant regexes and match them against
  the raw `resumes.extracted_text`; persist one `skill_evidence` row per
  matched skill with a quoted excerpt and a fixed confidence. Add
  `candidate_skills.status` (suggested/confirmed/rejected) as the user's
  review decision, where **"rejected" is a persistent tombstone** and the
  extractor **never writes that column at all**. Manual add resolves
  ONLY against the curated taxonomy (`skills.category IS NOT NULL`, or a
  known alias) and never coins a `skills` row.
- **Problem**: Turn stored resume text into evidence-backed skills with
  no model inference, without false positives from ordinary English, and
  — the hard part — in a way where re-running extraction can never undo
  a decision the user already made.
- **Alternatives**: (1) Deleting a candidate skill on rejection —
  rejected outright: extraction reruns, so the next run would faithfully
  recreate the row and the rejection would silently evaporate. The
  tombstone is the whole point. (2) A `DELETE` endpoint alongside
  reject — same failure mode, so there deliberately is none; rejection
  IS the removal mechanism. (3) An `is_manual` column — unnecessary,
  since the presence of a `manual` evidence row already answers it.
  (4) Letting manual add coin new skills the way Prompt 1.3's target
  skills do — rejected: a candidate skill is a scored, evidence-backed
  claim feeding Prompt 4.x, so founding one on a typo ("Pyton") or a
  duplicate ("ReactJS" beside React) would corrupt matching, and a
  self-coined skill has no aliases so the extractor could never match it
  anyway. (5) Case-sensitivity as the false-positive guard — rejected:
  resumes write "PYTHON"/"python" interchangeably, so case carries no
  signal and would cause false negatives.
- **Trade-off**: Ambiguous terms — taxonomy entries that are also
  ordinary English words — match ONLY in "list context" (delimiter on
  both sides), so "Languages: Python, Go" matches but "I go to the
  office" does not. That costs real recall: "I built services in Go"
  is missed. Accepted, because the product rule is that a skill must
  never be invented and the user can always add one by hand. The
  ambiguous set is currently just {go, express}; React/Jest/Agile were
  considered and excluded because in resume prose their skill sense
  dominates. It lives as a constant in app/skill_matching.py rather than
  a taxonomy column — a matching-strategy concern, not a vocabulary
  fact — with the understanding that past ~10 entries it should move
  into the taxonomy. Separately, a term's parts may be joined by
  whitespace OR punctuation but never punctuation-then-whitespace: a
  test caught that "unit. Testing" would otherwise match "Unit Testing"
  across a sentence boundary. The skills UI fetches on mount with an
  explicit Refresh rather than auto-updating when the worker finishes,
  which would have required coupling it to Prompt 2.2's ResumeSection.
- **Outcome**: `app/skill_matching.py` (pure, no I/O — all matching
  rules testable without fixtures) and `app/skill_extraction.py`
  (persistence, idempotency, reconciliation), called best-effort from
  the existing Celery worker after text extraction succeeds so a skill
  failure never flips a resume back to "failed". Migration
  `90da84c6fbb1`. Confidence is a fixed lookup: canonical 0.90, alias
  0.75, ambiguous-in-list-context 0.60, manual 1.00. Reruns are exact
  no-ops (verified live: a second and third run reported zero writes of
  any kind), guaranteed by ON CONFLICT DO NOTHING on candidate skills
  plus an evidence upsert whose WHERE clause suppresses unchanged
  writes; reconciliation only ever deletes UNREVIEWED suggestions with
  no evidence left. Endpoints `GET`/`POST /api/v1/candidate-skills` and
  `PATCH /{id}` (no DELETE). Covered by `tests/test_skill_matching.py`
  (53), `tests/test_skill_extraction.py` (15),
  `tests/test_candidate_skill_api.py` (23) and
  `skills-section.test.tsx` (10) — including a regression test that a
  "Rust" coined as a Prompt 1.3 target skill still returns 422 here.

- **Date**: 2026-08-24
- **Decision**: Ship fictional sample resumes as a *file generator*
  (`make sample-resumes` → `apps/api/var/samples/`) and no
  database seed/demo path at all.
- **Problem**: Prompt 2.5 needs the resume-to-skills flow to be
  demonstrable without real personal data. An upload stores the file on
  disk and its *full extracted text* in the `resumes` table, so a
  developer demoing with their own resume leaves a real name, address
  and phone number in every dev database it touches — and the only
  reason to reach for a real one is not having a safe one to hand.
- **Alternatives**: (1) a dev-only seed command inserting a ready-made
  user with pre-made resumes, candidate skills and evidence; (2) commit
  a couple of PDF/DOCX fixtures to the repo; (3) fixtures in the test
  suite only, with the manual demo left to improvise.
- **Trade-off**: A database seed would be *faster* to demo, and that is
  exactly what makes it the wrong tool: it bypasses upload → extraction
  → review, which is the flow the demo exists to show, so it would
  demonstrate the one part of the system nobody doubts (that rows can be
  displayed) while skipping the part that is actually interesting. It
  would also need a dev-only guard to avoid shipping an
  insert-arbitrary-users backdoor, for zero demo value. Committing
  binary fixtures avoids the generator but puts opaque blobs in git
  history that no reviewer can diff, and invites "just add mine". The
  generator costs one extra command before a demo and produces
  documents nobody can inspect in a pull request — accepted, because the
  *text* they are rendered from is a reviewable Python constant, and the
  end-to-end test asserts on that same constant.
- **Outcome**: `apps/api/scripts/sample_resumes.py` holds three
  fictional resumes as plain text plus the PDF/DOCX builders; `make
  sample-resumes` renders them to `apps/api/var/samples/` (already
  gitignored, so a generated document cannot be committed) and touches
  the database never. `tests/test_demo_end_to_end.py` enforces the
  fiction mechanically — RFC 2606 `example.com` emails, `555-01xx`
  phone numbers, an ASCII-only body, and a "not a real person" first
  line that survives the round trip — so a real resume pasted in fails
  CI rather than reaching a demo. The document builders were moved out
  of `tests/test_extraction.py` (which now aliases them) so the demo
  files and the tests are rendered by one implementation; the PDF
  builder gained per-line text operators and `/WinAnsiEncoding`, which
  a multi-line resume needs and a one-line test stub did not.

- **Date**: 2026-08-24
- **Decision**: Cover the demo with an API-level acceptance test
  (`tests/test_demo_end_to_end.py`) running Celery in eager mode, rather
  than adding Playwright or a real broker.
- **Problem**: Every existing test covers one slice — upload, or
  extraction, or matching, or the review endpoints. Nothing proved the
  slices join up, which is precisely the claim a demo makes.
- **Alternatives**: (1) Playwright driving a real browser against the
  real stack; (2) a Redis service in CI plus a real worker process; (3)
  extending `scripts/smoke.sh` with authenticated `curl` calls.
- **Trade-off**: The eager-mode test cannot prove broker delivery —
  `.delay()` executes in-process, so nothing demonstrates that a message
  published by the API reaches a separate worker. That is a real gap,
  stated in the test's own docstring and in `docs/demo.md` rather than
  papered over. A Redis service in CI would close it, at the cost of a
  worker process to supervise and poll for in every run; Playwright
  would additionally close the browser gap, at the cost of a heavy new
  dependency for a layer already covered by 19 component tests — the
  same reasoning that kept a headless browser out of `smoke.sh`. Neither
  buys enough to justify the weight at this stage.
- **Outcome**: One test walks sign-up → login → upload → extraction
  completes → evidence is present and *verbatim* → confirm one skill →
  reject another → re-run extraction → both decisions survive. The
  upload endpoint's `enqueue_extraction` is stubbed to a recorder and
  the task invoked explicitly: under eager mode that call would run
  `asyncio.run()` inside the TestClient's already-running event loop,
  and `enqueue_extraction`'s best-effort `try/except` would swallow the
  resulting RuntimeError, leaving the resume silently stuck at "queued".
  Skill names are asserted as a set *equality*, not a subset — a subset
  check would pass while the extractor invented a skill the document
  never mentions, which is the one failure the Evidence-First rule
  exists to prevent.

- **Date**: 2026-08-24
- **Decision**: Store a connected GitHub account with **no unique
  constraint on `github_user_id` or `username`** across users, and key
  the 1:1 relationship on `user_id` alone.
- **Problem**: Prompt 3.1 connects a *public* GitHub username. The
  obvious hygiene move is a global unique on the account id — one GitHub
  account, one CareerLens user.
- **Alternatives**: (1) `UNIQUE(github_user_id)`; (2)
  `UNIQUE(username)`; (3) a soft "claimed" flag with a dispute process.
- **Trade-off**: A unique constraint enforces an ownership claim that
  this prompt never verifies. There is no OAuth here — the API reads a
  public page and learns only that an account *exists*, not that the
  caller controls it. So a global unique would let whoever types
  `torvalds` first permanently prevent its real owner from connecting
  their own account: a denial-of-service wearing data-integrity clothes,
  and one with no recovery path short of manual database surgery. The
  cost of omitting it is that two users may reference the same public
  account, which is harmless — nothing about *reading public data*
  conflicts, and no scoring in Prompt 4.x is zero-sum between users.
  Revisit if OAuth ever makes ownership provable.
- **Outcome**: `github_connections` with `user_id` as both primary key
  and foreign key (the `profiles` shape). One connection per user is
  therefore a property of the schema, not of the handler remembering to
  delete first — pinned by a regression test that connects account A,
  connects account B, and asserts exactly one row survives with B's
  data. Disconnect is a hard DELETE rather than 2.4's tombstone, because
  nothing re-creates a connection automatically, so there is nothing for
  a tombstone to suppress. `github_user_id` (BigInteger) is stored
  alongside the username because GitHub usernames can be renamed and
  later recycled — ingestion in Prompt 3.2 keying on the string alone
  would silently start reading a stranger's repositories. Migration
  `a013618c2ddf`.

- **Date**: 2026-08-24
- **Decision**: Test the GitHub client at **two** levels — a fake client
  for the routes, and `httpx.MockTransport` for the client itself —
  rather than only swapping in a fake.
- **Problem**: The route needs to map every upstream failure to a status
  and a message, and the client needs to *produce* those failures from
  real HTTP conditions. A single fake satisfies the first and silently
  skips the second.
- **Alternatives**: (1) fake client only; (2) `respx` or a similar
  HTTP-mocking library; (3) an integration test hitting real GitHub.
- **Trade-off**: Two levels means two test files and a little
  duplication of scenario names. Worth it: with a fake alone, the
  timeout handling, the `X-RateLimit-Remaining` check and the payload
  validation never execute at all — the tests would assert that a canned
  `GitHubTimeout` becomes a 504, proving the route and nothing else.
  `respx` was rejected as an unnecessary dependency: `httpx.MockTransport`
  ships with httpx and does the same job. Hitting real GitHub was
  rejected outright — it makes CI depend on a third party, on network
  access, and on a 60-request/hour unauthenticated budget shared by
  every runner on the same egress IP.
- **Outcome**: `tests/test_github_client.py` drives the real client
  through a mock transport for the valid profile, 404, read and connect
  timeouts, 403-with-remaining-0, 429, an unparseable reset header, a
  plain 403 (which must NOT be reported as a rate limit), eight
  malformed payloads, a non-JSON body, 5xx and a connection error — plus
  an assertion that the outgoing request carries **no Authorization
  header**, which is the one regression that would otherwise make
  everything work better rather than worse. `httpx` moved from the dev
  group to a runtime dependency; it was already in the lock file via
  Starlette's TestClient, so the change added zero new packages
  (verified: `uv sync` resolved 65 and installed none).

- **Date**: 2026-08-24
- **Decision**: Reject unknown fields on the connect request
  (`extra="forbid"`), accepting that FastAPI's 422 body echoes the
  rejected value back to the sender.
- **Problem**: The product promise is "we never ask for your GitHub
  password". A client that sends one anyway should be told, not silently
  accommodated — but Pydantic's `extra_forbidden` error includes an
  `input` key containing the offending value.
- **Alternatives**: (1) `extra="ignore"` — drop unknown fields silently,
  echoing nothing; (2) an app-wide `RequestValidationError` handler that
  strips `input` from every 422.
- **Trade-off**: Silently ignoring a `password` field is worse
  messaging: nothing tells the caller they sent something the product
  does not want. The echo is reflected only to the client that supplied
  the value — it is never stored, never logged server-side, and never
  sent to GitHub — but it would reach a browser-side error reporter that
  captures response bodies. The app-wide scrubber is the right fix and
  is a strict improvement (no endpoint should echo submitted values),
  but it changes the 422 body of every existing endpoint, which is
  outside a slice scoped to the GitHub connection.
- **Outcome**: `extra="forbid"` kept. The limitation is asserted
  explicitly in `tests/test_github_connection_api.py` so it stays
  visible rather than being rediscovered, and the app-wide handler is
  left as a follow-up. Verified that nothing currently depends on the
  `input` key: no test reads it, and the web client reads only
  `detail[0].msg`.

- **Date**: 2026-08-24
- **Decision**: Cap one GitHub import at 20 repositories' worth of
  detail (`GITHUB_MAX_REPOSITORIES`), skipping forks, most recently
  pushed first — while still storing basic details for *every* listed
  repository.
- **Problem**: Unauthenticated GitHub allows 60 requests/hour per IP.
  Each repository costs two (languages + README), so 50 repositories
  would need 102 requests and simply cannot be imported within an hour.
- **Alternatives**: (1) no cap, and accept that large accounts fail
  partway; (2) cap the *listing* instead, so we never learn the true
  repository count; (3) require a GitHub token to lift the limit.
- **Trade-off**: A token would raise the ceiling to 5000/hour and remove
  the problem entirely — and is exactly what Prompt 3.1 ruled out, since
  it would mean asking for a credential to read data that is already
  public. Capping the listing would be cheaper but would destroy the
  honest "your account has N repositories" figure and, worse, would make
  deletion reconciliation unsafe. So the cap falls on detail fetching
  only: the listing is always walked in full, base rows are written for
  every repository (that data is already in the listing response and
  costs no extra request), and only languages/README are limited.
- **Outcome**: `repositories_available`, `repositories_forks_excluded`
  and `repositories_total` are three separate columns precisely so the
  UI can say "Imported the 20 most recently updated of your 47 public
  repositories" instead of "Imported 20 repositories", which a user
  reasonably reads as "I have 20". Whether the cap was hit is derived at
  the response boundary, not stored. Covered by `importSummary` unit
  tests in `github-section.test.tsx` and by
  `test_cap_limits_detail_to_the_most_recently_pushed`.

- **Date**: 2026-08-24
- **Decision**: GitHub rate limiting pauses an ingestion run at the RUN
  level and is never recorded as a per-repository failure.
- **Problem**: A run that hits the rate limit halfway through 20
  repositories has not failed at anything — it has been told to come
  back later. Treating it like a timeout would count healthy
  repositories as broken and, with a retry, re-spend requests on work
  already done.
- **Alternatives**: (1) count it in `repositories_failed` like any other
  upstream error; (2) fail the whole run and make the user restart.
- **Trade-off**: Handling it separately costs a resume marker
  (`github_repositories.detail_fetched_at`) that cannot be inferred from
  `updated_at`, because base rows are written for repositories whose
  detail was never fetched. Worth it: without it, a resumed run either
  re-fetches everything (spending the budget that was exhausted in the
  first place) or skips repositories it never actually read.
- **Outcome**: `GitHubRateLimited` is caught at the loop level, writes
  nothing for the in-flight repository, leaves the run `processing`, and
  returns a delay computed from GitHub's own `X-RateLimit-Reset`. The
  Celery task retries with `max_retries=None` for this case only —
  being throttled is not a failure of our code, and letting it burn the
  small transient-error budget would turn a wait into a failed import.
  Pinned by `test_rate_limit_pauses_the_run_and_preserves_progress` and
  `test_a_resumed_run_skips_repositories_already_handled`.

- **Date**: 2026-08-24
- **Decision**: Reconcile repository deletions ONLY from a listing that
  paginated to completion, and soft-delete rather than remove.
- **Problem**: The natural implementation — "anything not in this run's
  listing is gone" — is silently catastrophic when the listing was cut
  short by a timeout or a page cap. It would erase a user's entire
  history and look like correct cleanup code while doing it.
- **Alternatives**: (1) reconcile unconditionally; (2) hard-delete rows;
  (3) treat a per-repository 404 on a detail endpoint as proof of
  deletion.
- **Trade-off**: Making completeness explicit costs a field on the
  client's return type (`RepositoryListing.complete`) rather than
  inferring it from list length — deliberately, so the decision cannot
  be got wrong by accident. Option (3) is rejected for the same class of
  reason: a detail 404 is a race with the listing, not an authoritative
  absence, and a stray race must not do what only a complete listing may.
  Soft delete costs a `deleted_at` filter on every read query, and buys
  the ability for Prompt 3.3's evidence to stay explicable instead of a
  skill silently losing its support.
- **Outcome**: `test_incomplete_listing_never_soft_deletes_anything`,
  `test_a_detail_404_does_not_soft_delete_the_repository`, and
  `test_a_reappearing_repository_is_undeleted` are the three that hold
  this together.

- **Date**: 2026-08-24
- **Decision**: Store the README as truncated text + SHA + original byte
  size + a truncation flag, and store NO raw GitHub JSON payloads
  anywhere.
- **Problem**: "Store only the necessary raw snapshots for traceability"
  needs a definition, or it drifts into keeping every response body.
- **Alternatives**: (1) keep raw JSON for the profile, listing and
  languages responses; (2) store only a README hash; (3) store the
  README untruncated.
- **Trade-off**: A hash is smaller and tamper-evident, but Prompt 3.3
  must quote a verbatim excerpt as evidence and a hash cannot be quoted
  — that alone settles it. Raw JSON would duplicate columns we just
  normalized, re-introduce the personal fields (avatar, email, bio,
  follower counts) that Prompt 3.1 deliberately declined to store, and
  grow without bound. Untruncated README text has no size ceiling on
  third-party content.
- **Outcome**: The README is the single retained source snapshot.
  `readme_sha` makes it traceable and short-circuits the WRITE on a
  rerun — though not the request: reading the SHA requires fetching the
  README, and conditional requests (ETag) are out of scope for 3.2.
  Everything else is traceable through `github_repo_id` + `full_name` +
  `last_seen_at` on the repository and `started_at`/`attempt_count` on
  the run.

- **Date**: 2026-08-24
- **Decision**: For GitHub-derived skill evidence (Prompt 3.3): a
  separate database-only module (`app/github/skill_evidence.py`) invoked
  best-effort from the existing ingestion Celery task after a run reaches
  `succeeded` — NOT a second Celery task, and not code inside
  `run_ingestion`. It reuses `find_skill_matches()` and the curated
  taxonomy unchanged, adds four `ExtractionMethod` values, and needs no
  migration.
- **Problem**: Turn already-ingested repository data into evidence-backed
  skills without a second matcher, a second taxonomy, or any inference —
  and without the derived data being re-derivable only at the cost of
  GitHub's rate-limit budget.
- **Alternatives**: (1) Inline in `run_ingestion` — rejected: ingestion
  makes network calls, pauses for an hour under rate limiting and commits
  per repository, so a taxonomy change could only be reflected by
  re-spending ~42 of GitHub's 60 requests/hour re-fetching data already in
  Postgres, and a bug in skill code could drive `max_retries=None` GitHub
  retries. (2) A second Celery task chained after ingestion — rejected:
  `enqueue_*` is documented here as best-effort, so a second enqueue hop
  doubles the "Redis blip silently strands work" exposure to buy
  independent retry for an operation with nothing transient to retry.
  (3) GitHub-specific confidence values — rejected as the forbidden new
  ranking model; see the trade-off. (4) `github_repo_id` as
  `source_identifier` — rejected: a bare integer is unreadable to a human
  inspecting evidence, and `app/models/skill_evidence.py` already reserves
  "owner/repo" for this source type.
- **Trade-off**: **Confidence stays `CONFIDENCE_BY_KIND` unchanged**, so a
  language match and a README match can score identically. Deliberate:
  `confidence` answers "does this string denote this skill", not "how
  strong is this as evidence of ability" — the second question is Prompt
  4.x's, which already has `source_type` and `extraction_method` to weight
  by. Folding source strength in now would pre-empt 4.x inside a column
  Prompt 2.4 defined as a match-quality lookup. **Four extraction methods**
  mean up to four evidence rows per skill per repository; bounded by 3.2's
  20-repo cap, and worth it because `extraction_method` is the only field
  distinguishing signals from the same repository — one value would force
  an invented precedence rule for which excerpt wins. **Language evidence
  stores `excerpt = NULL`**, which is less informative to a human skimming
  but is the answer Prompt 2.3 pre-registered when it made the column
  nullable "because some evidence has no quotable text at all (a GitHub
  language statistic)": a language is a computed byte statistic, and
  "Python (82,341 bytes)" would be authored prose presented as a
  quotation. The byte counts stay inspectable in their own table.
  **Reconciliation is a full user-scoped sweep**, more aggressive than the
  resume path's narrow per-resume delete — justified because this
  extractor genuinely speaks for the user's whole account rather than one
  document, and guarded three ways (`source_type='github'`, user-scoped,
  and only keys absent from a freshly computed desired set). **README
  matching uses the truncated stored text**, so a skill mentioned only
  past 20k characters is missed; `readme_truncated` records the loss.
  **`source_identifier` is `String(255)` while `full_name` is
  `String(400)`** — GitHub's real ceiling is 39 + 1 + 100 = 140, so this
  is safe in practice but is a bounded assumption rather than a guarantee.
- **Outcome**: Four signals, each matched INDIVIDUALLY: README,
  description, one pass per topic, one pass per language (the union of
  `github_repository_languages` and `primary_language`, which is a member
  of that set rather than a separate signal — it is GitHub's own top entry
  from the same breakdown, but it arrives in the LISTING, so it is the only
  language signal available beyond the detail cap). Concatenating topics
  before matching was tested and rejected: the matcher tolerates
  whitespace between a term's parts, so topics ["unit", "testing"] joined
  for one pass match the skill "Unit Testing", which neither topic
  asserts — pinned by `test_topics_are_never_concatenated_before_matching`.
  Forks excluded (their description and topics still come from the
  listing, so the filter is load-bearing, not redundant with 3.2's);
  archived repositories INCLUDED, because archiving says "no longer
  maintained", not "not my work", and recency is 4.x's to weigh. Reruns
  are exact no-ops, asserted on row ids and timestamps.
  `scripts/extract_github_skills.py` / `make github-skills` re-derives for
  every connected user at zero request cost — the payoff of the
  separation. Covered by `tests/test_github_skill_evidence.py` (39),
  `tests/test_github_skill_worker.py` (5) and two new disconnect tests in
  `tests/test_github_connection_api.py`; 3.2's
  `test_ingestion_writes_no_candidate_skills_or_evidence` is retained
  unchanged and now proves the separation rather than merely documenting
  3.2's scope.

- **Date**: 2026-08-24
- **Decision**: Disconnecting GitHub PURGES GitHub-derived skill
  evidence, then removes only orphaned `suggested` candidate skills.
- **Problem**: Prompt 3.1 deferred this to 3.3. `docs/decisions.md`
  already accepts a dangling polymorphic reference for a deleted resume,
  so the consistent-looking choice was to leave GitHub evidence too.
- **Alternatives**: (1) Leave it dangling, matching the resume
  precedent; (2) delete every candidate skill that had GitHub evidence,
  reviewed or not.
- **Trade-off**: The resume precedent does not transfer, and the
  asymmetry is the whole argument: deleted-resume evidence is still
  RECONCILABLE, because the resume extractor may run again — whereas after
  a disconnect there is no connection and GitHub reconciliation will never
  run again. Leaving it would strand the user with skills citing
  repositories that no longer exist and no mechanism to remove them:
  unrecoverable, not merely dangling. Option (2) was rejected because it
  would silently discard a decision the user made by hand. The accepted
  cost is that a confirmed skill can survive with zero evidence — the
  existing, intended semantics (the user asserted it), identical to
  today's behaviour after a resume is deleted.
- **Outcome**: `purge_github_skill_evidence` is the same sweep as a
  normal run with an empty desired set, so resume evidence, manual
  evidence and every confirmed/rejected decision survive by construction
  rather than by a special case. Pinned by
  `test_disconnect_preserves_reviewed_skills_and_non_github_evidence`.

- **Date**: 2026-08-24
- **Decision**: The GitHub evidence reconciliation sweep keys on
  `(candidate_skill_id, source_identifier, extraction_method)`, not on
  `(source_identifier, extraction_method)`.
- **Problem**: Found by a test, not by review. The first implementation
  compared only the source and method, which reads correctly — until two
  skills are matched from the SAME repository through the SAME signal
  kind. They then share that pair, so as long as *any* skill still had,
  say, topic evidence for `ada/project`, every stale topic row for that
  repository survived the sweep. Reconciliation silently stopped working
  for any repository supporting more than one skill through one signal —
  which is the common case, not an edge case.
- **Alternatives**: Comparing the full evidence natural key in SQL via a
  tuple `NOT IN`; deleting and re-inserting all GitHub evidence each run.
- **Trade-off**: The diff is computed in Python over an explicit id list
  rather than as a SQL tuple `NOT IN`. The set is small and bounded by
  construction (3.2 caps a user at 20 detail-fetched repositories, times
  four methods, times a ~33-entry taxonomy), and for a DELETE, obviously
  correct beats clever. Delete-and-reinsert was rejected outright: it
  would churn `created_at` on every row every run, destroying the
  idempotency guarantee this design is built around.
- **Outcome**: `test_the_sweep_distinguishes_two_skills_from_one_signal`
  is the regression test, deliberately written with three skills from one
  repository's topics so it fails loudly against the old behaviour.

- **Date**: 2026-08-24
- **Decision**: Record — but do not fix — that an alias LONGER than its
  own skill's canonical name always wins the match.
- **Problem**: Discovered while writing Prompt 3.3's tests. A README
  saying "FastAPI" produces alias confidence (0.75), not canonical
  (0.90): `find_skill_matches` tries terms longest-first and each accepted
  match claims its span, so the alias "fast api" (8 characters) is tried
  before the canonical "FastAPI" (7) and the canonical spelling can never
  be reached. Prompt 2.4's own docstring says "strongest kind wins", which
  is true only among terms that do not overlap.
- **Alternatives**: Order terms by match kind before length; compare kind
  before honouring the span claim; drop the "fast api" alias.
- **Trade-off**: This is pre-existing Prompt 2.4 behaviour affecting
  resume extraction identically — 3.3 inherits it by reusing the matcher
  unchanged, which was an explicit constraint of this slice. Fixing it
  belongs in `app/skill_matching.py` and would change resume matching
  results, so it is out of scope here. The practical impact is small: one
  taxonomy entry is affected, and only its confidence, not whether the
  skill is found.
- **Outcome**: Pinned by
  `test_a_longer_alias_outranks_its_own_canonical_name` so it is a known,
  asserted property rather than a surprise, and
  `test_readme_canonical_match` uses Docker (which has no aliases) for its
  canonical assertion.

<!-- Add new entries above this line, most recent first. -->
