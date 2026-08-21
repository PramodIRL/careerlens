# CareerLens — Project Brief

## Product Definition

CareerLens is a responsive web app for early-career software candidates. A user
can create a profile, upload a resume, add a public GitHub username, save job
descriptions, inspect evidence-backed skills, receive explainable job matches,
identify skill gaps, and receive a learning roadmap.

CareerLens is not a chatbot and must not be a simple "resume + LLM API" wrapper.

## Product Rules (Evidence-First AI Rule)

- A score must be derived from stored, inspectable signals.
- The system must show evidence for each inferred skill and each matching
  conclusion.
- An LLM may structure extracted information and explain persisted results;
  it must never invent skills, work history, repository evidence, job
  requirements, or scores.
- A user can correct or remove extracted skills.
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

### Broader Project Scope

- **MVP / minimum portfolio project**: complete through 4.5.
- **Strong AI/ML portfolio project**: complete through 6.3 and 7.1–7.5.
- **Deployed portfolio project**: additionally complete 7.6–7.9.
