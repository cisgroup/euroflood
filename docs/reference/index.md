# API Reference

The complete public API. If you're new, start with [Getting Started](../getting-started.md)
and [Concepts](../concepts.md); this section is the exhaustive symbol-level
reference.

## Where things live

| You want to… | See |
|---|---|
| Discover events / hazard, download rasters | [API — floods, hazard, download](api.md) |
| Work with the object a query returns | [FloodFrame](floodframe.md) |
| Plot / explore / footprints / depth | [Visualization](visualization.md) |
| Configure via `EUROFLOOD_*` env vars | [Configuration](configuration.md) |
| Use the CLI | [CLI](cli.md) |
| Internals: query/hazard pipelines, repositories | [Pipelines](pipelines.md) · [Services](services.md) |
| Producer build pipeline | [Pipelines](pipelines.md) · [HPC Runbook](../hpc-runbook.md) |

## The public surface

Everything importable from the top-level `euroflood` package:

```python
import euroflood as ef

ef.floods, ef.hazard, ef.download          # queries + fetch
ef.mirror, ef.verify, ef.offline           # offline/HPC staging
ef.FloodFrame                                          # what floods()/hazard() return
ef.plot, ef.explore, ef.footprints                     # viz (needs [viz] extra)
ef.plot_depth, ef.explore_depth, ef.open_depth, ef.DepthRaster
ef.settings, ef.setup_logging                          # config + logging
```
