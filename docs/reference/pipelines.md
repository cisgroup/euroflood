# Pipelines

The pipelines split into **consumer** (query the index) and **producer** (build the
index). Most users only touch the consumer path indirectly, via
[`floods()`](api.md) / [`hazard()`](api.md).

## Consumer

The query pipelines behind the public API.

::: euroflood.pipelines.discovery.DiscoveryPipeline
    options:
        show_root_heading: true
        heading_level: 3

::: euroflood.pipelines.hazard.HazardPipeline
    options:
        show_root_heading: true
        heading_level: 3

## Producer

The build pipelines (see the [HPC Runbook](../hpc-runbook.md)).

::: euroflood.pipelines.mirror.MirrorPipeline
    options:
        show_root_heading: true
        heading_level: 3

::: euroflood.pipelines.ingestion.IngestionPipeline
    options:
        show_root_heading: true
        heading_level: 3

::: euroflood.pipelines.export.ExportPipeline
    options:
        show_root_heading: true
        heading_level: 3

::: euroflood.pipelines.doctor.IndexDoctor
    options:
        show_root_heading: true
        heading_level: 3
