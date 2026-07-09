# Services

Services handle specific domains of logic: networking, geospatial operations, the
index repositories, and data management.

## Index & lookups (consumer)

The repositories that resolve a query against the local or remote index.

::: euroflood.services.index_repository.IndexRepository
::: euroflood.services.dictionary_repository.DictionaryRepository
::: euroflood.services.events_repository.EventsRepository

## Geospatial

::: euroflood.services.location.LocationResolver
::: euroflood.services.geocoding.GeocodingService
::: euroflood.services.raster_ops.RasterOps
::: euroflood.services.hazard_tiles.HazardTileIndex

## Network & IO

::: euroflood.services.scraper.ScraperService
::: euroflood.services.downloader.DownloadService

## Data processing (producer)

::: euroflood.services.processor.RasterProcessor
::: euroflood.services.inventory.InventoryRepository

## Statistics

::: euroflood.services.statistics.depth_raster_stats
::: euroflood.services.statistics.catalogue_stats
::: euroflood.services.statistics.catalogue_summary
