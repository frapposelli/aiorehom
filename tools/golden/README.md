# Golden-vector generator

`generate.mjs` is a development tool, run by hand. It executes the vendor web
UI's JavaScript for the calculations that aiorehom reimplements in Python (the
`*_compat` functions in `aiorehom.logic`) on seeded inputs, and writes their
outputs to `tests/golden/*.json`. The Python tests then check the
reimplementations against those vectors.

**The vendor source is not included in this repository and must never be
committed.** The generator reads it from the directory named by
`$REHOM_WEBUI_SRC` and bundles it in memory with esbuild; only the generated
vectors (inputs and outputs, no code) are written. Each vector file records the
relative source file names it was generated from and their SHA-256 hashes.

To regenerate the vectors (Node.js 20 or later; the stored vectors were
generated with Node.js 22):

```sh
cd tools/golden
npm ci
REHOM_WEBUI_SRC=/path/to/vendor-web-ui-source npm run generate
```

The output is byte-identical across runs (seeded PRNG, no timestamps). With
`REHOM_WEBUI_SRC` set, `tests/golden/test_golden_freshness.py` also checks that
the recorded hashes still match the source; without it, that check is skipped.
