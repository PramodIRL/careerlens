# CLAUDE.md — Permanent Working Rules

These rules apply to every session working in this repository.

1. Work only on the requested prompt.
2. First inspect the existing repository and state a concise plan before making changes.
3. Do not edit unrelated files.
4. Use TypeScript strict mode and Python type hints.
5. Add tests for non-trivial behavior.
6. Do not add a dependency without explaining the concrete problem it solves.
7. Never hard-code, print, or request secrets. Secret variable names belong in `.env.example` only — never actual values.
8. Do not commit, push, create pull requests, deploy, or run destructive commands.
9. Prefer simple, readable code over unnecessary abstractions.
10. Before finishing, run relevant checks and report:
    - changed files
    - commands run
    - results
    - trade-offs
    - known limitations
    - a short interview explanation
11. Stop after the requested slice.
