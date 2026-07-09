"""The EuroFlood exception hierarchy.

All library errors derive from `EuroFloodError`, so callers can catch the
whole family with one ``except``. The CLI maps each subclass to a distinct exit
code and renders it as a clean one-line message (see the Output & Logging guide);
library/notebook users get the exception object with an actionable message.
"""


class EuroFloodError(Exception):
    """Base exception for all EuroFlood errors."""


class ConfigurationError(EuroFloodError):
    """Raised when configuration or paths are invalid."""


class NetworkError(EuroFloodError):
    """Raised when remote resources cannot be accessed after retries."""


class ScrapingError(NetworkError):
    """Raised when parsing the JRC website fails."""


class ProcessingError(EuroFloodError):
    """Raised when processing a raster fails."""


class GeocodingError(EuroFloodError):
    """Raised when a place name cannot be resolved."""


class CacheSchemaError(EuroFloodError):
    """Raised when the on-disk cache is missing or incompatible with the code."""


class HazardError(EuroFloodError):
    """Raised when GLOFAS hazard tiles can't be located, fetched, or mosaicked."""


class PublishError(EuroFloodError):
    """Raised when publishing the index bundle to a remote host fails."""


class SourceCoopError(PublishError):
    """Raised when uploading the index bundle to Source Cooperative fails."""


class VerificationError(EuroFloodError):
    """Raised when a published index fails end-to-end verification."""


class VisualizationError(EuroFloodError):
    """Raised when a catalogue/raster cannot be visualized (e.g. empty, unsupported)."""
