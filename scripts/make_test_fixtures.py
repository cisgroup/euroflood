"""Clip small REAL (CC-BY-4.0) samples from the local cache into tests/fixtures/realdata/.

Maintainer tool — run once to (re)generate the committed real-data test fixtures; it
is **not** part of the test run or CI. It needs a populated local cache (the default
``settings.cache_dir``, e.g. ``~/.cache/euroflood``) containing the published index
bundle plus at least one downloaded EFAS depth tile (``downloads/WD_MERGE_*.tif``).

Usage::

    python scripts/make_test_fixtures.py                 # read from settings.cache_dir
    python scripts/make_test_fixtures.py --cache ~/.cache/euroflood

Outputs (all tiny, redistributable under CC-BY-4.0 with attribution — see
tests/fixtures/realdata/ATTRIBUTION.md):
  - efas_depth_clip.tif        real EFAS depth crop (native metric CRS, cm, nodata 0)
  - nuts_subset.geojson        real Eurostat NUTS regions + an appended Zutphen boundary
  - index/…                    a real index bundle clipped to a small ROI (Zutphen)
"""

from __future__ import annotations

import argparse
import glob
import shutil
import unicodedata
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.windows import Window, from_bounds

from euroflood.config import get_settings
from euroflood.core.manifest import write_manifest

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "realdata"

# A small ROI over Zutphen on the IJssel (lon/lat, EPSG:4326) — enough real flood
# events to query, and wide enough to cover the tutorials' bbox/point/buffer demos.
# The place "Zutphen" is a municipality, absent from NUTS, so its boundary is appended
# to nuts_subset separately (see ``append_zutphen_boundary``) for offline geocoding.
_ZUTPHEN_BOUNDS = (6.05, 52.00, 6.40, 52.25)
# Real place/region names whose NUTS polygons cover the test areas. Köln + Valencia
# stay so the existing integration geocoder tests keep resolving; the Dutch names add
# the Zutphen region context (Gelderland is the level-filter demo).
_NUTS_WANTS = (
    "valencia",
    "comunitat valenciana",
    "comunidad valenciana",
    "espana",
    "koln",
    "cologne",
    "nordrhein",
    "deutschland",
    "gelderland",
    "achterhoek",
    "nederland",
)


def _strip(text: str) -> str:
    """Lowercase + strip accents (mirrors the geocoder's name normalisation)."""
    nfkd = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def make_depth_clip(cache: Path) -> None:
    """A small crop of a real EFAS depth tile, centred on its wet cluster."""
    tiles = sorted(
        glob.glob(str(cache / "downloads" / "*.tif")),
        key=lambda p: Path(p).stat().st_size,
    )
    if not tiles:
        raise SystemExit(f"No downloaded depth tiles in {cache / 'downloads'}")
    src = tiles[0]
    size = 384
    with rasterio.open(src) as s:
        arr = s.read(1)
        wet = (arr > 0) & (arr != (s.nodata or 0))
        ys, xs = np.where(wet)
        # Pick the `size`-aligned block with the most wet pixels (they're scattered,
        # so a centroid window can land dry).
        from collections import Counter

        blocks = Counter(zip((ys // size).tolist(), (xs // size).tolist(), strict=True))
        (by, bx), _ = blocks.most_common(1)[0]
        r0, c0 = by * size, bx * size
        h, w = min(size, s.height - r0), min(size, s.width - c0)
        win = Window(c0, r0, w, h)
        data = s.read(1, window=win)
        prof = s.profile.copy()
        prof.update(
            height=h,
            width=w,
            transform=s.window_transform(win),
            compress="deflate",
            tiled=False,
        )
        prof.pop("blockxsize", None)
        prof.pop("blockysize", None)
        out = OUT / "efas_depth_clip.tif"
        with rasterio.open(out, "w", **prof) as dst:
            dst.write(data, 1)
        n_wet = int((data > 0).sum())
        assert n_wet > 0, "clip window contains no wet pixels — widen it"
        print(
            f"depth clip: {w}x{h} EPSG:{s.crs.to_epsg()} {data.dtype} nodata={s.nodata} "
            f"wet_px={n_wet} -> {out.stat().st_size / 1024:.1f} KB  (from {Path(src).name})"
        )


def make_nuts_subset(cache: Path) -> None:
    """A few real Eurostat NUTS regions covering the test places."""
    nuts = cache / "boundaries" / "NUTS_RG_20M_2024_4326.geojson"
    if not nuts.exists():
        raise SystemExit(f"NUTS dataset not found at {nuts}")
    gdf = gpd.read_file(nuts)
    namecol = next(
        c for c in ("NAME_LATN", "NUTS_NAME", "NAME", "name") if c in gdf.columns
    )
    norm = gdf[namecol].map(_strip)
    keep = norm.apply(lambda n: any(w in n for w in _NUTS_WANTS))
    sub = gdf[keep].copy()
    # simplify to shrink the file; keep only the columns the geocoder reads
    cols = [
        c
        for c in ("NUTS_ID", "LEVL_CODE", "NAME_LATN", "NUTS_NAME")
        if c in sub.columns
    ]
    sub = sub[[*cols, "geometry"]].copy()
    sub["geometry"] = sub.geometry.simplify(0.01)
    out = OUT / "nuts_subset.geojson"
    sub.to_crs("EPSG:4326").to_file(out, driver="GeoJSON")
    print(
        f"nuts subset: {len(sub)} features -> {out.stat().st_size / 1024:.1f} KB  "
        f"names={sorted(sub[namecol].tolist())}"
    )


def append_zutphen_boundary() -> None:
    """Append a simplified Zutphen municipality polygon to ``nuts_subset.geojson``.

    Zutphen is a municipality, not a NUTS region, so the offline ``local`` geocoder
    cannot resolve it from NUTS names. We fetch its OSM/Nominatim boundary once (served
    from the on-disk geocode cache if present, else the network) and append it as a
    named feature so ``floods("Zutphen")`` resolves offline in the tutorials/tests.
    """
    from euroflood.config import get_settings
    from euroflood.services.geocoding import GeocodingService

    s = get_settings()
    s.geocoder_backend = "online_first"  # Nominatim, or the cached .wkb geometry
    s.boundary_dataset_path = None
    geom = GeocodingService(settings=s).get_geometry("Zutphen, Netherlands")
    geom = geom.simplify(0.001)  # shrink 2000+ pts to a small ring

    out = OUT / "nuts_subset.geojson"
    sub = gpd.read_file(out)
    row: dict = {c: None for c in sub.columns if c != "geometry"}
    for c in ("NAME_LATN", "NUTS_NAME"):
        if c in sub.columns:
            row[c] = "Zutphen"
    if "LEVL_CODE" in sub.columns:
        row["LEVL_CODE"] = 3
    if "NUTS_ID" in sub.columns:
        row["NUTS_ID"] = "NL225ZUT"  # synthetic id (Zutphen sits in NUTS3 NL225)
    row["geometry"] = geom
    merged = gpd.GeoDataFrame(
        pd.concat([sub, gpd.GeoDataFrame([row], crs=sub.crs)], ignore_index=True),
        crs=sub.crs,
    )
    merged.to_file(out, driver="GeoJSON")
    print(
        f"appended Zutphen boundary ({len(geom.exterior.coords)} pts) "
        f"-> nuts_subset now {len(merged)} features, {out.stat().st_size / 1024:.1f} KB"
    )


def make_index_bundle(cache: Path) -> None:
    """A real index bundle (COG + dict + events + manifest) clipped to a small ROI."""
    bundle = OUT / "index"
    bundle.mkdir(parents=True, exist_ok=True)
    idx = cache / "europe_flood_index.tif"
    with rasterio.open(idx) as s:
        win = from_bounds(*_ZUTPHEN_BOUNDS, s.transform)
        data = s.read(1, window=win)
        prof = s.profile.copy()
        prof.update(
            height=data.shape[0],
            width=data.shape[1],
            transform=s.window_transform(win),
            compress="deflate",
            tiled=True,
            blockxsize=256,
            blockysize=256,
        )
        with rasterio.open(bundle / "europe_flood_index.tif", "w", **prof) as dst:
            dst.write(data, 1)

    combos = np.unique(data[data != 0]).tolist()
    d = pd.read_parquet(cache / "flood_dictionary.parquet")
    dsub = d[d["combo_id"].isin(combos)].reset_index(drop=True)
    dsub.to_parquet(bundle / "flood_dictionary.parquet", index=False)

    fids = np.unique(np.concatenate(dsub["flood_ids"].to_numpy())).tolist()
    e = pd.read_parquet(cache / "events.parquet")
    esub = e[e["global_id"].isin(fids)].reset_index(drop=True)
    esub.to_parquet(bundle / "events.parquet", index=False)

    write_manifest(bundle / "manifest.json")
    meta = cache / "dictionary_meta.json"
    if meta.exists():
        # Copied verbatim, so its ``n_combos`` is the producer cache's GLOBAL combo count
        # for whichever index version built it, not the count in this clipped bundle. No
        # test asserts it; the doctor (its only reader) is never pointed at this fixture.
        shutil.copyfile(meta, bundle / "dictionary_meta.json")

    total = sum(p.stat().st_size for p in bundle.glob("*"))
    print(
        f"index bundle: {len(combos)} combos, {len(fids)} flood_ids, {len(esub)} events "
        f"-> {total / 1024:.1f} KB  (COG {data.shape[1]}x{data.shape[0]})"
    )


def main() -> None:
    """Clip all three real fixtures from the local cache into tests/fixtures/realdata/."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="Cache dir to clip from (default: settings.cache_dir).",
    )
    parser.add_argument(
        "--skip-depth",
        action="store_true",
        help="Skip regenerating efas_depth_clip.tif (keep the committed Valencia clip).",
    )
    args = parser.parse_args()
    cache = args.cache or get_settings().cache_dir
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"Clipping real fixtures from {cache} -> {OUT}")
    if not args.skip_depth:
        make_depth_clip(cache)
    make_nuts_subset(cache)
    append_zutphen_boundary()
    make_index_bundle(cache)
    print("Done. Remember: these are CC-BY-4.0 — keep ATTRIBUTION.md.")


if __name__ == "__main__":
    main()
