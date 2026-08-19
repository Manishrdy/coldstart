# Coldstart

Automated job sourcing, filtering, and LLM-based resume matching pipeline.
See [`scope.md`](scope.md) for the *why* and [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md)
for the *how* (module-by-module build plan).

## Setup

```bash
uv sync
cp .env.example .env   # fill in required values
uv run python scripts/init_db.py
```

## Development

```bash
uv run pytest
uv run ruff check .
```

Full setup instructions, cron examples, and how to inspect the `errors`
table will be filled in as the pipeline is built out (see
`DEVELOPMENT_PLAN.md` §22, Definition of Done).
