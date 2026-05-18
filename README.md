# Aarva

AI-narrated, AI-curated journalism. A reboot of Curio's editorial promise on a fully automated curation engine.

Working name: **Aarva**.

## Project documents

Read these in order to understand what we're building:

1. **`docs/Editorial Promise.pdf`** — editorial guidelines. Source of truth for the editorial sensibility.
2. **`docs/aarva_kickoff.docx`** — the living kickoff document. Three audience lenses, five content pillars, four jobs-to-be-done, the full curation pipeline, and the open-questions register. Updated as decisions resolve.
3. **`docs/aarva_architecture_v1.md`** — v0.1 build architecture. Stage modules, data model, provider abstractions, build sequence.
4. **`docs/aarva_prompts_v1.md`** — drafted LLM prompts for Stages 4, 5, 6, 8a, 8b.
5. **`docs/aarva_calibration_set_v1.md`** — 32 hand-labelled articles for Stage 4 tonal-filter calibration.
6. **`docs/aarva_prototype_v2.html`** — current visual identity prototype.

## Build status (v0.1)

In progress. See `aarva/` directory for the in-progress build, and `docs/decisions/` for major design decisions made along the way.

Target: working daily-edition pipeline (RSS in → LLM-scored articles → assembled edition → TTS audio → web page + podcast RSS feed out) in ~10 days. No personalisation yet; that's v0.2.

## Running locally

(Setup instructions added once Day 1 lands.)

## Workflow

- `main` branch holds the latest stable code.
- Daily build work happens on `day-N` branches.
- Major design decisions are documented in `docs/decisions/` as one-page ADRs.
- The kickoff doc (`docs/aarva_kickoff.docx`) is the source of truth for resolved decisions; the open-questions register lives in §5.

## Repository structure

```
/                                  Repo root
├── README.md                      This file
├── .gitignore
├── docs/
│   ├── Editorial Promise.pdf
│   ├── aarva_kickoff.docx
│   ├── aarva_architecture_v1.md
│   ├── aarva_prompts_v1.md
│   ├── aarva_calibration_set_v1.md
│   ├── aarva_prototype_v2.html
│   └── decisions/                 ADRs for major design choices
└── aarva/                         The Python package
    ├── config/                    YAML configs
    ├── sources/                   RSS / scraping
    ├── stages/                    Pipeline stage modules
    ├── clients/                   LLM and TTS provider abstractions
    ├── output/                    Web renderer + RSS feed generator
    ├── scripts/                   Utility scripts (calibration, voice trials)
    ├── tests/
    └── daily.py                   Top-level pipeline orchestrator
```

## License

Private project, no public license yet.
