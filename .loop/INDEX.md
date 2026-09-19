| id | title | outcome | iters | verif-reject | tokens | key learning | link |
|----|-------|---------|-------|---------------|--------|--------------|------|
| 9 | password auth | merged | unreliable (see retro) | 2 | not captured | full-suite + 3x repeat run caught a regression and a ~25%-flaky test the implementer's own green suite missed | .loop/9/retro.md |
| 11 | persist threads | merged | 7 implementer passes (16 journal entries) | 6 | not captured | store-vs-HTTP test gap and a JS/backend split hid unmet criteria behind a green suite; a shared bounds-check function replaced two rounds of per-route patches | .loop/11/retro.md |
