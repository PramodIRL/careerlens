# CareerLens — Project Brief

## Product Definition

CareerLens is a responsive web app for early-career software candidates. A user
can create a profile, upload a resume, add a public GitHub username, save job
descriptions, inspect evidence-backed skills, receive explainable job matches,
identify skill gaps, check their academic and experience eligibility against a
posting's stated requirements, and receive a learning roadmap.

CareerLens is not a chatbot and must not be a simple "resume + LLM API" wrapper.

## Product Rules (Evidence-First AI Rule)

- A score must be derived from stored, inspectable signals.
- The system must show evidence for each inferred skill and each matching
  conclusion.
- An LLM may structure extracted information and explain persisted results;
  it must never invent skills, work history, repository evidence, job
  requirements, or scores.
- A user can correct or remove extracted skills.
- **Absence of data is never failure.** A qualification the candidate has not
  declared is UNKNOWN, and a requirement the posting hedged open ("or a
  related field") is UNDETERMINED. Neither may be reported as "does not
  qualify". The system must never invent a CGPA, marks, a degree, a
  graduation year, or years of experience — including by defaulting an
  unstated value to zero.
- **A CGPA scale is the one declared default.** An explicitly written scale
  always wins ("8.2/10" → 10, "3.6/4.0" → 4); where none is written the scale
  is 10. The default is *stored and shown*, never applied invisibly, so a
  reader can see what was assumed. It supplies a scale only — never a value.
- The MVP is complete at transparent matching and skill gaps. Embeddings,
  LLMs, caching, and deployment come afterward.

## Architecture Direction

- **Web**: Next.js App Router, TypeScript strict mode, Tailwind, accessible
  semantic HTML.
- **API**: FastAPI, Pydantic, SQLAlchemy 2 async, Alembic, pytest, Ruff.
- **Data**: PostgreSQL with pgvector; Redis and Celery only for processing
  that should not block a request.
- **Development**: Docker Compose runs data services. The API and web app
  should also run locally outside Docker for easier debugging.
- **Storage**: local files behind an interface during development;
  S3-compatible object storage only for production.
- **AI**: provider-agnostic adapters. Mock providers in tests. No real
  provider credentials until the core app works.

## Architecture Flow

```
Next.js + TypeScript + Tailwind web app
  → FastAPI REST API
  → PostgreSQL + pgvector / Redis when needed
  → SQLAlchemy + Alembic / background worker for slow ingestion
  → resume parsing / GitHub ingestion / skill evidence / job matching / roadmap
  → semantic retrieval + constrained LLM explanations later
```

## MVP Boundary

The minimum portfolio MVP is complete through **Prompt 4.5**. It must
demonstrate:

```
user profile → resume extraction → GitHub evidence → saved jobs
  → ranked matches → score breakdown → skill gaps
```

Do not start embeddings or LLM explanations in Prompt 4.5.

## Eligibility Domain (Prompt 5.1)

Approved scope beyond the MVP. Job postings state two different kinds of
requirement, and CareerLens models them as two separate domains that are
never combined into one number:

```
SavedJob                          Candidate
 ├── job_skill_requirements        ├── candidate_skills / skill_evidence
 │     └── skill_match_v1          │     └── skill_match_v1
 │         skill_gap_v1            │         skill_gap_v1
 └── job_eligibility_requirements  └── candidate_qualifications
       └── eligibility_v1                └── eligibility_v1
```

The product must be able to say **"Skill match: 82%"** and, separately:

```
Eligibility:
✓ CGPA requirement satisfied
✗ Class 12 requirement not satisfied
? Graduation year unknown
```

Rules for this domain:

- **Eligibility never feeds `skill_match_v1` or `skill_gap_v1`.** It does not
  cap, weight or multiply the skill score, and the two are versioned
  independently.
- **Eligibility has no percentage.** A CGPA floor and a degree requirement are
  not commensurable; the result is a flag plus a per-requirement breakdown.
- **Four states, never collapsed**: SATISFIED, NOT_SATISFIED, UNKNOWN (the
  candidate has not told us) and UNDETERMINED (the posting cannot be
  evaluated as written). Only NOT_SATISFIED makes a candidate ineligible.
- **Work authorization is out of scope for 5.1** — not modelled, not
  extracted, not stored. The phrasings are open-ended and the things that
  correlate with a person's right to work are exactly what a product must
  never infer it from.
- **Extraction is deterministic** — regex over a curated vocabulary. No LLM,
  no embeddings, no semantic inference. Ambiguous prose produces no
  requirement rather than a guess.
- Every stored requirement carries a **verbatim excerpt** from the posting.

Delivered in two slices: **5.1a** the job-side requirements, the
user-declared candidate facts and the resolver; **5.1b** resume-derived
qualification facts with provenance and a suggested/confirmed/rejected review
state. Years of experience stays user-declared in both: deriving it means
summing employment ranges across overlaps, gaps and internships, which is
inference rather than extraction.

**The qualification profile is user-declared and authoritative.** It belongs
to the USER, not to a job: one profile, entered once, compared against every
saved job's requirements. Only `confirmed` facts count towards an eligibility
verdict — a value nobody has asserted reads as UNKNOWN, in either direction.

Skill matching remains the primary experience; eligibility is supporting
information and sits below the score and the gaps on a saved job.

5.1b's deterministic resume qualification extractor is retained but **not
wired to the resume worker**: normal resume processing stays resume → skills
and evidence. It is kept dormant and tested for a future explicit "import
from resume" feature, which would write `suggested` facts for the user to
accept rather than silently claiming to know things about them.

### Broader Project Scope

- **MVP / minimum portfolio project**: complete through 4.5.
- **Eligibility domain**: 5.1a (job requirements + declared facts), then 5.1b
  (resume-derived facts).
- **Strong AI/ML portfolio project**: complete through 6.3 and 7.1–7.5.
- **Deployed portfolio project**: additionally complete 7.6–7.9.
