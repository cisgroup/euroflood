# Releasing EuroFlood

EuroFlood has **two independently versioned artifacts**:

| Artifact | Version source | Published to | Automated? |
|---|---|---|---|
| **Library** (`euroflood` on PyPI) | `pyproject.toml` `version` | PyPI **and Zenodo** | Yes — both, on a `v*` git tag |
| **Index** (the data bundle) | `--version` at publish time | Source Cooperative (+ Zenodo DOI) | No — a deliberate local command |

The library and the index carry **separate Zenodo DOIs** and must not be conflated:

| | Concept DOI (always latest) |
|---|---|
| Software | `10.5281/zenodo.22837458` |
| Index dataset | `10.5281/zenodo.21284459` |

They are decoupled: a library release does not re-publish the index, and vice-versa. A
released library pins the immutable index prefix it reads via `DEFAULT_INDEX_BASE_URL`
(`src/euroflood/_data.py`).

## Release the library to PyPI

Development happens in this **private** repo (`cisgroup/CODE-EuroFlood`); the library is
**published from a public mirror** (`cisgroup/euroflood`). `scripts/publish_public.sh` pushes
a clean, self-contained snapshot + a `vX.Y.Z` tag to the mirror, where `release.yml` publishes
to PyPI (Trusted Publishing) and `docs.yml` deploys the docs. The private repo never publishes
or hosts docs itself (both workflows are guarded by `if: github.repository == 'cisgroup/euroflood'`).

1. Move `CHANGELOG.md`'s `## [Unreleased]` items into a new `## [X.Y.Z]` section.
2. Bump `pyproject.toml` (`project.version = "X.Y.Z"`), then run `uv lock` so `uv.lock`
   records the new project version.
3. Update `CITATION.cff` `version` + `date-released` (not machine-checked — bump by hand).
4. Commit, open a PR, merge to `main` (CI green).
5. Mirror to the public repo from a clean `main`:
   ```bash
   scripts/publish_public.sh --dry-run   # inspect the file manifest; pushes nothing
   scripts/publish_public.sh             # push the snapshot + vX.Y.Z tag to cisgroup/euroflood
   ```
   The mirror's `release.yml` runs the full gate, **fails if the tag ≠ the `pyproject`
   version**, builds the sdist + wheel, and publishes to PyPI via **Trusted Publishing
   (OIDC)** — no stored token. `docs.yml` deploys to <https://cisgroup.github.io/euroflood/>.

The mirror is a **snapshot**, not a fork: each release wipes-and-copies (one commit + one tag,
no dev history), so direct commits to the public repo are overwritten next release — triage
public PRs/issues and apply the fix upstream here.

### The Zenodo leg

`release.yml`'s `zenodo` job runs **after** the PyPI job and archives the *same* `dist/`
artifacts, so PyPI and Zenodo hold byte-identical files and a DOI is never minted for a
release that failed to reach PyPI. It calls
`scripts/zenodo_software_release.py --concept-recid 22837458`, which resolves the concept
record to the latest published version and adds a new one — the concept DOI keeps
resolving to the newest release, so `CITATION.cff` never needs a per-release DOI edit.

Nothing to do by hand. To rehearse, run the script locally with `--sandbox --dry-run`.

A published Zenodo record **cannot be deleted**, which is why the job sits last.

### One-time setup (before the first release)
- Create the **public repo** `github.com/cisgroup/euroflood` (empty — the mirror fills it) and
  make sure your SSH key can push to it. Turn on **GitHub Pages** (source: GitHub Actions).
- Confirm the distribution name `euroflood` is free on PyPI (pick another name in
  `pyproject.toml` if it is taken).
- On PyPI, add a **trusted publisher**: repo `cisgroup/euroflood`, workflow
  `release.yml`, environment `pypi`.
- Create a GitHub **environment** named `pypi` on the public repo (Settings → Environments).
- Create a GitHub **environment** named `zenodo` on the public repo, holding a
  `ZENODO_TOKEN` secret (Zenodo → Applications → Personal access tokens, scopes
  `deposit:write` + `deposit:actions`). This is the project's **only** stored secret —
  PyPI uses OIDC and needs none — so keep it environment-scoped rather than repo-wide.
- Recommended: rehearse once against **TestPyPI** before the first real publish.

## Publish a new index version

The bundle is produced on HPC (`euroflood build-index`; see the
[HPC Runbook](docs/hpc-runbook.md)). Publishing uploads that built bundle.

1. Put the Source Cooperative **temporary AWS credentials** in `.env` (gitignored, never
   committed):
   ```
   AWS_ACCESS_KEY_ID=...
   AWS_SECRET_ACCESS_KEY=...
   AWS_SESSION_TOKEN=...
   ```
   (Product page → **View Credentials** → *Environment Variables*. They expire — re-fetch
   if an upload fails with a credentials error.)
2. Install the publish extra and upload (validate → stamp → upload → self-verify):
   ```bash
   uv sync --extra publish                                    # or: pip install "euroflood[publish]"
   euroflood publish --source-coop --version X.Y.Z            # live /vsicurl host
   euroflood publish --zenodo --version X.Y.Z                 # + citable DOI (needs ZENODO_TOKEN)
   euroflood publish --source-coop --version X.Y.Z --dry-run  # rehearse: validate + print the plan only
   ```
3. If the version prefix changed, bump `DEFAULT_INDEX_BASE_URL` in `src/euroflood/_data.py`
   so a fresh install reads the new bundle, then cut a library release.
4. Check the live index any time:
   ```bash
   euroflood verify remote                          # uses the baked / configured URL
   EUROFLOOD_RUN_ONLINE_TESTS=1 uv run pytest -m online
   ```

## Dataset-card / README surfaces

There is **one** maintained dataset card — `src/euroflood/pipelines/product_readme.md` —
rendered by `build_readme()` and shipped to **both** the Source Cooperative product landing
page (repo-root `README.md`) and the Zenodo record. The repository's top-level `README.md`
is the GitHub / PyPI landing page (a different audience). Keep the dataset description in the
template, not duplicated per host.
