# Demoing resume-to-skills without real personal data

How to show the Prompt 2.1–2.4 flow — upload a resume, watch it get
extracted, inspect the evidence behind each skill, correct what's wrong —
using fictional documents only.

This walkthrough covers the resume half only. For the whole Phase 4 MVP —
GitHub evidence, saved jobs, match scores and skill gaps — see
[phase-4-acceptance.md](phase-4-acceptance.md), which continues from
where this one stops.

**Never demo with a real resume.** Not your own, not a friend's, not one
from a job board. An uploaded document is stored on disk and its full
extracted text is written to the `resumes` table, where it stays until
someone deletes it. A demo audience does not need a real person's name,
address and phone number on screen, and a dev database is not a place to
put them. `make sample-resumes` exists so there is never a reason to.

## 1. Generate the fictional resumes

```bash
make sample-resumes
```

This writes four documents to `apps/api/var/samples/`:

| File | Who | Why it's in the set |
| --- | --- | --- |
| `backend-engineer.pdf` | Ada Sample | The main demo document — a dense, unambiguous resume that yields 12 skills |
| `frontend-engineer.docx` | Rio Placeholder | The second file format, plus alias matching (`k8s` → Kubernetes at lower confidence) |
| `career-changer.pdf` | Sam Invented | A sparse resume whose prose contains deliberate traps — see step 6 |
| `campus-graduate.pdf` | Priya Placeholder | Carries an EDUCATION block (CGPA, Class X/XII, degree, branch, graduation year) and "Fresher" — the fixture for the dormant qualification extractor; uploading it extracts skills only |

Every name, employer, school, email and phone number in them is invented.
Emails use `example.com` (reserved by RFC 2606); phone numbers use the
`555-01xx` range reserved for fiction; each document says in its own first
line that it is not a real person.

The command writes **files only**. It creates no users, no resumes and no
skills, and it never touches the database — the demo below goes through
the real upload and extraction path, the same code a real user hits.
`apps/api/var/` is gitignored, so a generated document cannot be
committed; regenerate them whenever you need them rather than storing them
anywhere.

## 2. Start the stack

Four terminals (see the README for details):

```bash
make start
```

```bash
make start-api
```

```bash
make start-worker
```

```bash
make start-web
```

`make start-worker` is the one that's easy to forget — without it, an
uploaded resume sits at "Queued" forever and nothing else in the app
complains.

If this is a fresh database, apply migrations and seed the taxonomy first:

```bash
make migrate && make seed-skills
```

Skill extraction matches against the curated taxonomy, so an unseeded
database produces an upload that succeeds and zero skills.

## 3. Sign up

Open <http://localhost:3000>, register, and log in. Use a fictional
address here too — `ada.sample@example.com` matches the document you're
about to upload. Any password of the minimum length works; it's a local
dev database.

## 4. Upload

On the dashboard, choose `apps/api/var/samples/backend-engineer.pdf`.

The row appears as **Queued**, flips to **Processing…**, then settles on
**Ready** — the page polls every 2 seconds while a resume is pending and
stops once it isn't. If it stays at Queued, the worker isn't running.

## 5. Show the evidence

Press **Refresh** in the Skills section. (Skills appear once the worker
finishes; the section fetches on mount and on demand rather than polling —
a known limitation, noted in `docs/decisions.md`.)

Twelve skills appear under **Needs review**, each with its evidence shown
inline — never hidden behind a click:

> **From your resume** · 90% confidence
> *Languages: Python, SQL*

The points worth making out loud:

- **The excerpt is a verbatim line from the document.** Nothing is
  summarised, reworded or generated. You can scroll the PDF and find that
  exact line. No LLM is involved anywhere in this path — matching is
  literal string matching (`apps/api/app/skill_matching.py`).
- **Confidence comes from how the skill matched**, not from a model's
  self-assessment: 0.90 for a canonical name, 0.75 for an alias, 0.60 for
  an ambiguous term found in a list.
- **Upload `frontend-engineer.docx` too** to show the alias path: `k8s`
  becomes Kubernetes at 75%, sitting next to canonical matches at 90%.

## 6. Show what it refuses to infer

Upload `career-changer.pdf`. It yields exactly three skills — Python, Git,
Flask — from a document that also contains:

> Former secondary school teacher moving into software. I go to a local
> coding meetup most weeks, and express an interest in backend work in
> particular.

Neither **Go** nor **Express** is suggested. Both are real taxonomy
skills, and both appear as words on the page; they're rejected because
they're in prose, not in a list. A demo that only ever shows what the
system *finds* is half a demo — this is the half that shows it doesn't
invent.

## 7. Correct it

Ada's resume lists Django under SKILLS, but every experience bullet is
FastAPI — the kind of stale line a real resume accumulates.

- **Confirm** FastAPI.
- **Reject** Django.

Both move into their own sections. Rejected skills stay visible with a
**Restore** button and keep their evidence, so the reason they were ever
suggested is still inspectable. There is no hard delete, by design: if
rejecting removed the row, the next extraction run would faithfully
re-suggest the skill and silently discard the correction.

To prove that, re-upload the same PDF. The new upload extracts normally —
and FastAPI is still confirmed, Django still rejected. **The user's
decision outranks the extractor, permanently.**

## 8. Clean up

Delete the resumes from the dashboard when you're done. Nothing here is
sensitive, but leaving a demo database tidy means the next person to
demo starts from the same place you did.

---

## What's automated, and what isn't

`apps/api/tests/test_demo_end_to_end.py` runs steps 3–7 as an acceptance
test: sign up, upload, extraction completes, evidence is present and
verbatim, confirm one skill, reject another, re-run extraction, both
decisions survive. It also asserts mechanically that every sample resume
is fictional — `example.com` emails, `555-01xx` numbers — so a real
resume pasted into `scripts/sample_resumes.py` fails CI rather than
reaching a demo.

Two things that test deliberately does **not** cover:

- **Redis broker delivery.** It runs Celery in eager mode, so the task
  executes in-process. It proves the extraction pipeline (claim, extract,
  persist, match, write evidence); it does not prove that a message
  published by the API reaches a separate worker process. Only running
  the real stack — step 2 above — shows that, which is part of why this
  walkthrough exists.
- **The browser.** It exercises the API, not the dashboard. The web
  layer has its own tests (`resume-section.test.tsx`,
  `skills-section.test.tsx`) against mocked responses of the same shape.
  Nothing but this walkthrough puts the two halves together in a real
  browser.

Run it with:

```bash
cd apps/api && uv run pytest tests/test_demo_end_to_end.py
```
