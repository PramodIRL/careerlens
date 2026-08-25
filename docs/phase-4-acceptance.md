# Phase 4 MVP — acceptance walkthrough

The MVP boundary in `docs/project-brief.md` is one sentence:

> user profile → resume extraction → GitHub evidence → saved jobs
> → ranked matches → score breakdown → skill gaps

This document is how that sentence gets signed off: a 5–10 minute live
demo, the arithmetic to check it against by hand, and the checklist the
Phase 4 release is judged on.

**Everything here uses invented data.** No real resume, no real GitHub
account, no network call to github.com. The two fixture modules —
`apps/api/scripts/sample_resumes.py` and
`apps/api/scripts/sample_github.py` — are asserted to be fictional by
`tests/test_mvp_acceptance.py`, so a real person's details pasted into
either one fails the suite rather than reaching a demo.

---

## 1. Prepare

```bash
make start && make migrate && make seed-skills && make sample-resumes
```

Then four terminals:

```bash
make start-api
```

```bash
make start-worker
```

```bash
make start-web
```

`make start-worker` is the one that's easy to forget. Without it an
uploaded resume sits at **Queued** forever and nothing else complains.

---

## 2. The demo, step by step

| # | Do this | Point out |
|---|---|---|
| 1 | Register at <http://localhost:3000/register> as `ada.sample@example.com` | An `example.com` address, reserved by RFC 2606 |
| 2 | Upload `apps/api/var/samples/backend-engineer.pdf` | The row shows **Queued**, then **Processing…**, then **Ready** — a real Celery task over a real Redis broker |
| 3 | Look at **Your skills** | 12 skills, all *Needs review*. Each shows a **verbatim** line from the PDF — nothing summarised, no LLM anywhere in this path |
| 4 | Note the skill profile: Resume 12, GitHub 0 | The provenance counters, before any GitHub data exists |
| 5 | `make demo-github EMAIL=ada.sample@example.com`, then **reload the page** | The fictional account is imported through the real ingestion pipeline with zero network requests |
| 6 | Look at **Your GitHub** | `octofictional`, 4 repositories, *"Imported all 3 … 1 fork was not included"* |
| 7 | Find **Linux** in Your skills | Its only evidence is `octofictional/deploy-notes`. Ada's resume never mentions Linux — **GitHub added a skill the resume could not** |
| 8 | Find **borrowed-toolkit** in the repository list | It is a fork, its description says *"A Kubernetes deployment toolkit"*, its topics say `kubernetes`. Kubernetes is credited to **nobody**: forks are never credited |
| 9 | Save the fictional job below | |
| 10 | Expand **View match** | 8 requirements extracted, at three levels |
| 11 | Read the score out loud and check it on paper | See §3 — **74%**, 14 of 19 weighted points |
| 12 | Read the gap buckets | See §4 — three non-satisfying states, never collapsed into one "missing" list |
| 13 | Confirm **PostgreSQL** in Your skills | Needs-confirmation drops 3 → 2, satisfied rises 3 → 4, and **the score stays 74%** — see §5, this is correct |
| 14 | Reject **Redis** | Score drops **74% → 68%**, 13 of 19. No manual Refresh needed |
| 15 | Edit the job (§6) and save | Requirements reconcile, score rises **68% → 80%**, the required gap disappears |

### The fictional job

Company `Nowhere Systems`, title `Backend Engineer`, description:

```
Backend Engineer at Nowhere Systems (fictional).

Python is required for this role. PostgreSQL is required. Candidates
should be able to write SQL. Kubernetes is required.

Docker experience is preferred. FastAPI is preferred. Familiarity with
Linux is preferred.

We also use Redis here.
```

It is built to exercise every branch at once: all three requirement
levels, one requirement stated as a **capability** rather than a keyword
("Candidates should be able to write SQL" → SQL *required*), one skill
only GitHub can supply (Linux), and one that is genuinely missing
(Kubernetes).

---

## 3. The score, by hand

`skill_match_v1`. Weights: required = 3, preferred = 2, mentioned = 1.

| Requirement | Level | Weight | Candidate state | Counts? |
|---|---|---:|---|---:|
| Python | required | 3 | confirmed | +3 |
| PostgreSQL | required | 3 | suggested | +3 |
| SQL | required | 3 | confirmed | +3 |
| Kubernetes | required | 3 | no row at all | 0 |
| Docker | preferred | 2 | rejected | 0 |
| FastAPI | preferred | 2 | confirmed | +2 |
| Linux | preferred | 2 | suggested (GitHub only) | +2 |
| Redis | mentioned | 1 | suggested | +1 |

```
earned      = 3 + 3 + 3 + 2 + 2 + 1            = 14
obtainable  = 3 + 3 + 3 + 3 + 2 + 2 + 2 + 1    = 19
overall     = round(14 / 19 * 100)             = 74
```

Required coverage **3 / 4** — reported separately because
`skill_match_v1` applies **no penalty** for a missing required skill. A
job can look healthy at 74% while a hard requirement is unmet, and that
is exactly why the panel says *"Missing 1 required skill: Kubernetes"*
in its own row rather than burying it in the percentage.

The UI prints `Scored by skill_match_v1: 14 of 19 weighted points`, so
the arithmetic above is checkable directly off the screen.

---

## 4. The gap buckets

| Bucket | Contents | Why |
|---|---|---|
| `required_gaps` | Kubernetes | No candidate row exists. Evidence list is **empty**, never an invented "no evidence found" sentence |
| `preferred_gaps` | — | |
| `informational_gaps` | — | |
| `needs_confirmation` | Linux, PostgreSQL, Redis | Real evidence exists; only the user's review is missing. **Not** an ordinary gap — telling someone to go learn a skill they have already demonstrated would be wrong |
| `rejected_requirements` | Docker | The user's own decision, shown with the evidence they disowned rather than reported back as an absence |
| satisfied | Python, SQL, FastAPI | |

`1 + 0 + 0 + 3 + 1 + 3 = 8` — every requirement lands in exactly one
place. `/match` and `/gaps` are two readings of one shared resolver
(`app/matching/resolve.py`), so they cannot disagree.

---

## 5. Why confirming a skill does not move the score

This surprises people, so say it before someone asks.

`skill_match_v1` already counts a **suggested** skill as satisfied. The
evidence is real and stored; the only thing missing is the user's
review. So confirming PostgreSQL changes the *review* state — needs-
confirmation 3 → 2, satisfied 3 → 4 — and correctly leaves the score at
74%.

**Rejecting** is what moves it, because rejection is a claim about the
candidate rather than about the review queue: rejecting Redis drops
earned weight 14 → 13 and the score 74% → 68%.

---

## 6. The job edit

Replace the description with:

```
Backend Engineer at Nowhere Systems (fictional).

Python is required for this role. PostgreSQL is required. Candidates
should be able to write SQL. Docker is required. Git is required.

FastAPI is preferred. Familiarity with Linux is preferred.

We also use Redis here.
```

One edit, all three of Prompt 4.2's reconciliation operations:

- **deleted** — Kubernetes is gone from `job_skill_requirements`, not
  left behind as a stale row
- **updated in place** — Docker moves preferred → required, same id
- **inserted** — Git is new, and it is already satisfied from both
  sources

```
earned      = 3 + 3 + 3 + 0 + 3 + 2 + 2 + 0    = 16
obtainable  = 3 + 3 + 3 + 3 + 3 + 2 + 2 + 1    = 20
overall     = round(16 / 20 * 100)             = 80
```

Required coverage 4 / 5, `required_gaps` is now empty, and the two
rejected requirements are Docker and Redis.

---

## 7. Screenshot checklist

Seven shots, in this order:

1. **Candidate profile with resume-derived skills** — 12 skills, each with a verbatim excerpt
2. **GitHub evidence and provenance** — the repository list with the fork marked *"Fork · Basic details only"*
3. **Saved job and its extracted requirements** — all three levels, including the capability-cue SQL row
4. **Match score and breakdown** — 74%, Required 3/4, `14 of 19 weighted points`
5. **Skill gaps** — all five buckets
6. **Updated state after confirming a skill** — needs-confirmation 3 → 2, score unchanged
7. **Final end-to-end state after the job edit** — 80%, no required gaps

Keep them out of the repository; `apps/api/var/` is gitignored if you
need somewhere local to put them.

---

## 8. What is automated, and what is not

`apps/api/tests/test_mvp_acceptance.py` runs steps 1–15 as an acceptance
test, asserting every number in §3, §4 and §6 exactly — not `score > 0`,
which would pass on an implementation that scored everything at 1%.

```bash
cd apps/api && uv run pytest tests/test_mvp_acceptance.py
```

Three things it deliberately does **not** cover:

- **Redis broker delivery.** It runs Celery eagerly and stubs both
  enqueue calls, so it proves the resume and GitHub *pipelines*, not
  that a message published by the API reaches a separate worker
  process. Only the walkthrough above shows that.
- **The browser.** It exercises the API. The dashboard has its own
  component tests against mocked responses of these exact shapes.
- **The real GitHub API.** By design — see `scripts/sample_github.py`.

---

## 9. Known limitations

Observed during the Phase 4 acceptance run, not hypothetical.

- **Requirement extraction is deterministic phrase matching**, not
  understanding. It reads cue words near a skill name
  (`app/job_requirements/classify.py`). A posting that expresses a
  requirement in a way the cue lists do not cover is read as a passing
  mention.
- **Mentioned-skill noise is real.** Any recognised skill name in the
  text becomes at least a `mentioned` requirement carrying weight 1, so
  a posting that name-drops a technology in passing still moves the
  denominator.
- **The job-side and resume-side extractors disagree about confidence
  for the same spelling.** The description literally says `FastAPI`, and
  the stored requirement records `matched_term = "fast api"` at 0.75
  (the alias path) while resume evidence for the identical spelling
  records 0.90. Confidence is not an input to `skill_match_v1`, so no
  score is affected — but the job panel under-reports how cleanly that
  requirement matched.
- **`make demo-github` writes outside the browser session**, so the page
  must be reloaded before the connection and its evidence appear. It is
  a developer command, not a UI action.
- **Re-importing is idempotent for everything that matters, but not
  literally a no-op.** A second import adds no duplicate repository, no
  duplicate evidence and no new candidate skill, and never changes a
  confirmed or rejected decision — but `github_repositories.last_seen_at`
  and `updated_at` do move on every run, because Prompt 3.2 records
  "we saw this repository again" unconditionally
  (`app/github/ingestion.py`). Worth knowing before someone diffs the
  table and thinks the import is churning.
- **Access tokens last 15 minutes.** A demo that pauses can hit a `401`
  on the next click; reload the dashboard to pick up a fresh token
  before continuing.
- **Confirming a suggested skill does not change the score** (§5).
  Correct, and worth saying out loud before it looks like a bug.
- No embeddings, no semantic similarity, no LLM explanations, no
  learning roadmap, no academic or salary eligibility. All deliberately
  out of the Phase 4 boundary.

---

## 10. Phase 4 release checklist

### Product
- [ ] Profile loads, edits and persists
- [ ] Resume upload → queued → processing → ready
- [ ] Candidate skills appear with resume evidence
- [ ] GitHub connection and repository import succeed
- [ ] GitHub evidence attaches to canonical candidate skills
- [ ] Saved jobs create, edit and delete
- [ ] Job requirements extract at all three levels, capability cues included
- [ ] `/match` returns the hand-calculated score
- [ ] `/gaps` returns the expected five buckets
- [ ] Confirm / reject / job-edit propagate with no manual Refresh

### Data and explainability
- [ ] Every candidate skill traces to a stored evidence row
- [ ] Every job requirement traces to a verbatim description excerpt
- [ ] The score is reproducible by hand from the response
- [ ] A genuinely missing skill carries an empty evidence list
- [ ] Nothing in any response is generated prose

### Security
- [ ] Another user gets 403 on the job, its requirements, its match and its gaps
- [ ] Unauthenticated requests get 401
- [ ] A request body naming `user_id` is rejected with 422, not silently ignored
- [ ] No secret, token or credential appears in the repo or in any log

### Quality gates
- [ ] `uv run pytest tests/test_mvp_acceptance.py`
- [ ] `make test` (full API and web suites)
- [ ] `make lint`
- [ ] `make typecheck`
- [ ] `make format-check`
- [ ] `uv run alembic check`

### Scope
- [ ] No embeddings
- [ ] No LLM explanations
- [ ] No semantic ranking, roadmap or eligibility features
