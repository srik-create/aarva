# ADR 0001 — v0.1 project architecture

**Status:** Accepted
**Date:** 2026-05-18

## Context

Aarva v0.1 is the first build of the curation pipeline. Major editorial design decisions (Q1, Q4, Q5, Q21, Q32) have been resolved through the design phase. The build needs an architecture that is fast to ship, modular enough to evolve as we learn from real output, and committed to the editorial design we've locked in.

## Decision

Five commitments shape the v0.1 architecture:

1. **Modular by stage.** Each pipeline stage is an independent module behind a typed interface. Swapping an implementation is one file.
2. **Configuration over code.** All editorial parameters (allowlist, weights, thresholds, prompts, slot structure, TTS provider) live in YAML configs. No code changes to tune.
3. **State is queryable.** SQLite holds all article state, scores, fingerprints, edition history. Every pipeline decision is logged and inspectable.
4. **Stages run independently.** Each stage invokable in isolation for testing and debugging. The orchestrator wires them but doesn't hide them.
5. **Provider-agnostic interfaces.** `LLMClient` and `TTSClient` abstractions mean Claude Code, Anthropic API, Piper, F5-TTS, ElevenLabs all swap by config.

## Consequences

- **Faster iteration.** Editorial tuning happens in YAML, not Python.
- **Provider portability.** v0.1 ships on Claude Code + Piper. Day-one swap to Anthropic API + ElevenLabs is config-only.
- **Inspectable failure modes.** When the daily edition contains a piece that shouldn't be there, the run log shows exactly which stage admitted it and with what score.
- **Some upfront cost.** Writing the abstractions takes a half-day. Worth it because we have a year of expected design evolution ahead.

## Reference

Full architecture spec: `docs/aarva_architecture_v1.md`.
