"""Guards that the documented public API surface stays in sync with the code.

These are cheap "did the docs drift?" checks: every advertised name must exist,
FloodFrame must keep its documented methods, and every Settings field must carry a
description (the Configuration reference renders from those).
"""

import pytest

import euroflood as ef


def test_all_exports_are_importable():
    for name in ef.__all__:
        assert hasattr(ef, name), f"euroflood.__all__ advertises missing {name!r}"


def test_unknown_attribute_raises_attributeerror():
    # The PEP 562 __getattr__ rejects anything not in _VIZ_EXPORTS.
    with pytest.raises(AttributeError, match="no attribute 'does_not_exist'"):
        getattr(ef, "does_not_exist")  # noqa: B009 (bare access triggers __getattr__)


def test_headline_functions_present():
    for name in ("floods", "hazard", "download", "mirror_hazard", "setup_logging"):
        assert callable(getattr(ef, name))


def test_floodframe_documented_methods():
    from euroflood import FloodFrame

    for method in (
        "download",
        "plot",
        "explore",
        "footprints",
        "stats",
        "summary",
        "depths",
    ):
        assert callable(getattr(FloodFrame, method)), method
    assert isinstance(FloodFrame.files, property)  # the downloaded-paths accessor


def test_viz_names_served_lazily():
    # Exposed via PEP 562 __getattr__ from euroflood.viz (no eager heavy import).
    for name in ("plot", "explore", "footprints", "plot_depth", "open_depth"):
        assert callable(getattr(ef, name))
    assert isinstance(ef.DepthRaster, type)


def test_every_setting_has_a_description():
    from euroflood.config import Settings

    missing = [n for n, f in Settings.model_fields.items() if not f.description]
    assert not missing, f"Settings fields missing a description: {missing}"
