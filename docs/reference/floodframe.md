# FloodFrame

`FloodFrame` is what [`floods()`](api.md) and [`hazard()`](api.md) return — a thin
`geopandas.GeoDataFrame` subclass (so all pandas/geopandas operations work) with a
few convenience methods for downloading and visualizing the catalogue.

::: euroflood.pipelines.discovery.FloodFrame
    options:
      show_root_heading: true
      heading_level: 2
      members:
        - download
        - files
        - depths
        - stats
        - summary
        - plot
        - explore
        - footprints
