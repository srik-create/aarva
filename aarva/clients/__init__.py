"""Provider-agnostic clients for the external services Aarva depends on.

Each external dependency is behind an abstract base class with multiple
concrete implementations. Swapping a provider is a single config change.

Currently in this module:
  - EmbeddingClient    — for vectorising article text (consolidation +
                          personalisation share this representation).
  - LLMClient          — for tonal/classification/fingerprint scoring and
                          hook/contextualisation generation. [Day 3]
  - TTSClient          — for synthesising audio. [Day 6]
"""
