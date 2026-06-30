# filepage_waterline

Reconstruct and visualize file page cache residency lifecycles from ftrace SQLite data.

The main tool is in `outputs/fscache_residency.py`. It rebuilds page-cache residency from `add`, `access`, and `delete` events, then emits a standalone HTML viewer with two color modes:

- file-based coloring
- `pid_name`-based coloring

See `outputs/README_fscache_residency.md` for usage and options.
