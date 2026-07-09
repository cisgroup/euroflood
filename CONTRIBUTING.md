# Contributing to EuroFlood

Thanks for your interest! This guide covers the dev setup, the quality gate, and
our docstring conventions.

## Setup

EuroFlood uses [uv](https://docs.astral.sh/uv/) for environments and packaging.

```bash
git clone https://github.com/cisgroup/euroflood
cd euroflood
uv sync --extra viz          # install core + dev + visualization deps
uv run pre-commit install    # enable the commit hooks (ruff, whitespace, ...)
```

## The quality gate

CI runs these on every push/PR; all must pass. Run them locally before opening a
PR (this is exactly what `.github/workflows/ci.yml` does):

```bash
uv run ruff format --check .     # formatting
uv run ruff check .              # lint (incl. Google-style docstring rules)
uv run mypy src                  # strict type checking
uv run deptry .                  # dependency hygiene
uv run pytest --cov=euroflood    # tests + coverage (must stay >= 90%)
uv run mkdocs build --strict     # docs build (no warnings)
```

`uv run pre-commit run --all-files` runs the fast subset (ruff + whitespace) on the
whole tree.

## Tests

- Tests live in `tests/` and mirror the `src/euroflood/` layout.
- New code needs tests; **coverage must stay ≥ 90%** (branch coverage is on).
- Visualization tests (`tests/unit/viz/`) skip automatically without the `[viz]`
  extra; CI installs it so they run.
- Keep tests offline — mock network (`mock_requests_get`) and build tiny fixtures
  (`built_index`, `sample_tif_path`) rather than hitting real services.

## Docstring conventions

- **Google style**, enforced by Ruff (`D` rules, `convention = "google"`).
- Write a one-line imperative summary ("Return…", "Query…", "Plot…").
- Document non-obvious `Args:` / `Returns:` / `Raises:`. Types come from the
  annotations — don't repeat them as `name (type):`.
- Public / high-traffic functions should carry an `Examples:` block. Mark examples
  that need network or a built index with `# doctest: +SKIP`.
- Modules get a docstring that orients the reader (what it does, where it fits).

## Docs

```bash
uv run mkdocs serve          # live-preview at http://127.0.0.1:8000
```

Narrative docs live in `docs/`; the API reference is generated from docstrings via
mkdocstrings. When you add a public symbol, add it to the relevant
`docs/reference/*.md` page. Docs images are pre-rendered and committed under
`docs/images/` (the build runs offline).

## Commit / PR

- Branch from `main`; keep the gate green.
- Small, focused PRs with a clear description are easiest to review.
