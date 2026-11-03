"""The AI career assistant and resume tailor (2026-11-03).

OPT-IN, BRING YOUR OWN KEY. These are the only features in the product
that call a hosted model, and only when the user has supplied an
OpenRouter key (per request, from the browser) or the operator has set
the server fallback. The explanation and roadmap prose paths are
untouched and still use the mock or local Ollama providers. See
docs/decisions.md (2026-11-03) for the privacy trade-off.

    openrouter.py  credentials + the one HTTP client, errors made safe
    tools.py       read-only tools over the CALLER's own rows
    agent.py       the bounded tool-calling loop behind /ai/chat
    tailor.py      the resume tailor behind /ai/tailor

THE MODEL READS, IT NEVER WRITES. Every tool calls an existing read
handler or query with the authenticated user, so ownership is enforced
by the same code that enforces it for the rest of the API, and no tool
can insert, update or delete a row.
"""
