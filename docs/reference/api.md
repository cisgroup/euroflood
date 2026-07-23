# API: floods, hazard, download

The top-level functions for discovering flood events and hazard layers and fetching
their rasters. All are re-exported from `euroflood` (e.g. `euroflood.floods`).

The object these return is a [`FloodFrame`](floodframe.md).

## Discover historic floods

::: euroflood.api.floods
    options:
      show_root_heading: true
      heading_level: 3

## Query global flood hazard

::: euroflood.api.hazard
    options:
      show_root_heading: true
      heading_level: 3

## Download rasters

::: euroflood.api.download
    options:
      show_root_heading: true
      heading_level: 3

## Mirror data for offline use

::: euroflood.api.mirror
    options:
      show_root_heading: true
      heading_level: 3

## Verify a local mirror

::: euroflood.api.verify
    options:
      show_root_heading: true
      heading_level: 3

## Toggle offline mode

::: euroflood.api.offline
    options:
      show_root_heading: true
      heading_level: 3

## Logging

::: euroflood.logging.setup_logging
    options:
      show_root_heading: true
      heading_level: 3
