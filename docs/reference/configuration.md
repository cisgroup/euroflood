# Configuration

All configuration is centralized in a single `Settings` object, exposed as the
`euroflood.settings` singleton. Every field is set by an `EUROFLOOD_<FIELD>`
environment variable (or by mutating `euroflood.settings` at runtime).

```python
import euroflood as ef

ef.settings.output_dir = "results/"     # runtime override
# equivalently:  export EUROFLOOD_OUTPUT_DIR=results/
```

The table below (from the field descriptions) documents every setting and its
`EUROFLOOD_*` variable.

::: euroflood.config.Settings
    options:
      show_root_heading: true
      heading_level: 2
