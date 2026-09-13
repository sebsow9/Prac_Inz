# H264-THESIS

Workspace pracy inżynierskiej o bezpieczeństwie i szyfrowaniu H.264/AVC.

Pliki startowe:

- `.hermes.md` — reguły kontekstu ładowane przez Hermesa tylko w tym workspace.
- `EXPERIMENT.md` — szablon reprodukowalnego eksperymentu.
- `.gitignore` — ochrona przed przypadkowym commitem sekretów i dużych artefaktów.

Zalecany podział po utworzeniu repozytorium:

- `src/` — kod,
- `configs/` — wersjonowane parametry eksperymentów bez sekretów,
- `scripts/` — automatyzacja,
- `docs/` — tekst i notatki,
- `references/` — metadane źródeł,
- `data/` — wejścia lokalne lub manifesty,
- `results/` — surowe wyniki lokalne,
- `artifacts/` — wygenerowane media.

Duże pliki wideo i wyniki trzymaj poza zwykłym Git; jeśli muszą być wersjonowane, świadomie skonfiguruj Git LFS lub zewnętrzny storage wraz z checksums i manifestem.
