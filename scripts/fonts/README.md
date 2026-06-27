# Bundled fonts

Files in this directory are third-party fonts used by build-time
scripts (currently just `scripts/generate_pwa_icons.py`). They're
checked in so icon generation is reproducible without an internet
round-trip.

## Fraunces-Bold.ttf

- **Source:** [Google Fonts — Fraunces](https://fonts.google.com/specimen/Fraunces) (the same family the web app loads on every page via Google Fonts CDN).
- **Author:** Phaedra Charles, Flavia Zimbardi, Lasko Dzurovski — under [The Fraunces Project](https://github.com/undercasetype/Fraunces).
- **Licence:** [SIL Open Font License, Version 1.1](https://scripts.sil.org/OFL). Free to use, modify, embed, and redistribute (including bundled in repositories like this one) so long as it's not sold by itself.
- **Variant fetched:** the v38 weight-700 static cut Google Fonts serves to browsers (URL embedded in `scripts/generate_pwa_icons.py`'s docstring history).
- **Update procedure:** Google Fonts versions its CDN URLs (`v38`, `v39`, …); if Aarva's icon design ever wants a newer cut, refetch the latest TTF from the current CSS URL and overwrite this file.

Other fonts (when added) should follow the same pattern: original-name file + a section here noting source, licence, and variant.
