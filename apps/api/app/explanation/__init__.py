"""Structured, validated LLM explanations of a persisted match (Prompt 6.1).

THE LLM EXPLAINS; IT NEVER DECIDES. Every number a reader sees comes
from app/matching/score.py and is echoed out of the persisted facts —
nothing in this package can change `overall_score`, mark a requirement
satisfied, coin a skill, or author evidence. The model receives facts
and returns prose about them; anything it returns that cannot be traced
back to those facts is REJECTED rather than shown.

The pipeline is one direction, with a validator at the end:

    facts.py     persisted rows      -> ExplanationFacts
    prompt.py    ExplanationFacts    -> ExplanationRequest (facts as DATA)
    provider.py  ExplanationRequest  -> raw text
    validate.py  raw text + facts    -> MatchExplanation, or ExplanationRejected
    adapter.py   the whole chain, fail-closed

NO CREDENTIAL AND NO HOSTED PROVIDER. The only implementation is a
deterministic mock. There is no API key read, requested or named
anywhere in this package.
"""
