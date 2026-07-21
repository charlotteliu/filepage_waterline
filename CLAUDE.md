# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

Reconstruct and visualize Linux **file page-cache residency lifecycles** from ftrace data. The pipeline has two independent halves:

1. **Ingest** (`load_ftrace_to_db_file.py`, repo root): parses raw ftrace text logs (plus `smaps`, `inode.txt`, ark disassembly, step-info) into a SQLite DB via regexes, one table per event type.
2. **Visualize** (`outputs/fscache_residency.py`): reads that DB read-only, reconstructs residency intervals, and emits a **standalone self-contained HTML viewer** (vanilla JS + `<canvas>`, no external/CDN dependencies — data is embedded as inline JSON).

The two halves are decoupled by the SQLite schema. Most work happens in the visualizer; the ingest script depends on data paths and a `script/` package that are **not checked into this repo**.

## Commands

```bash
# Generate a deterministic demo DB (no real ftrace data needed)
python3 outputs/generate_top_apps_demo_db.py --out outputs/top_apps_fscache_demo.db

# Build the HTML viewer from a DB (main tool)
python3 outputs/fscache_residency.py \
  --db outputs/top_apps_fscache_demo.db \
  --html outputs/out.html \
  --json outputs/out.json

# Fast iteration on a large DB: window + filter to cut event volume
python3 outputs/fscache_residency.py --db <db> \
  --start 80 --end 140 --file-like '%/data/%' --max-lanes 400 --html out.html
```

There are no tests, linters, or build config. `python3` is the only runtime (repo verified on 3.9). `pandas` is imported by `load_ftrace_to_db_file.py` only — the visualizer and demo generator use the stdlib alone (`sqlite3`, `argparse`, `json`).

### Key `fscache_residency.py` flags
- `--start/--end` — timestamp window (seconds).
- `--file-like` / `--pid-like` — SQL `LIKE` filters on `inode_mapping.filename` / `pid_name`.
- `--no-access` — reconstruct residency from add/delete only; skip `access` events (which reassign a page's owning `pid_name`).
- `--max-lanes` / `--max-heatmap-files` / `--max-groups` / `--max-coldmap-files` — cap how much detail survives into each chart.
- `--bucket-seconds` / `--target-points` — aggregate-curve resolution (auto-targets ~260 points).

## Architecture of the visualizer

The core model: a page is identified by `(dev, ino, ofs)`. Events drive a state machine over resident intervals:
- `mm_filemap_add_to_page_cache` → opens a resident interval.
- `mm_filemap_delete_from_page_cache` → closes it.
- `mm_filemap_access_history` → does **not** change residency, but reassigns the interval's current owning `pid_name` (this is what powers the "color by pid_name" mode vs. "color by file" mode).

`main()` (`fscache_residency.py:1694`) runs **two passes over the event stream** (events are merged from the add/delete/access tables, ordered by timestamp, via `iter_events`):
- **Pass 1 — `pass_for_peaks`**: finds peak-residency moments and selects the top-N files, `pid_name`s, and file+pid groups to color, plus heatmap files.
- **Pass 2 — `reconstruct`**: rebuilds the aggregate residency curve, per-page lifecycle lanes, and the heatmap, keyed to the groups chosen in pass 1.

Pass 2 also computes the **whole-machine cold/hot treemap** (`coldmap` payload): per-file `footprint` (resident page-seconds = ∫ resident-page-count dt, integrated incrementally via `flush_file`), `peak` pages, access count, and `density` (accesses per page-second). The viewer renders it as a squarified treemap where **area = residency footprint** and **color = access density** (blue=cold → red=hot), so files that hold a lot of cache but are rarely accessed show up as large blue tiles (dashed-outlined when in the top-quartile-area / bottom-quartile-temperature "big & cold" set).

The result dict feeds `write_outputs`, which substitutes it into `HTML_TEMPLATE` (a big raw-string near line 820) at the `__DATA__` placeholder. **The viewer is one Python string containing HTML+CSS+JS** — edit the template in place; there is no separate front-end build.

**Anomaly counters** (`delete_without_active_add`, `access_without_active_add`, `duplicate_add_closed_previous`) are tallied during reconstruction and surfaced in the output; nonzero values usually mean the trace window is truncated or events were dropped, not a code bug.

## SQLite schema (contract between the two halves)

Tables are created/dropped in `init_database` (`load_ftrace_to_db_file.py:81`). The visualizer only requires `mm_filemap_add_to_page_cache` and `mm_filemap_delete_from_page_cache` (and `mm_filemap_access_history` unless `--no-access`); `inode_mapping` (`dev,ino → filename,size`) and `timestep` (business-step reference lines) are optional enrichment. Event tables carry `(dev, ino, page, pfn, ofs, mmapcnt, flags, timestamp, pid, pid_name)`.

When adding a new event source, add its regex + table in the ingest script **and** teach `fscache_residency.py` to read it — the schema is the only coupling point.

## Notes / gotchas
- `outputs/` contains committed sample artifacts (`sample_fscache_residency.*`, `top_apps_fscache_demo.*`) — regenerating them overwrites those files.
- `load_ftrace_to_db_file.py` `main()` has hardcoded Windows-style input paths and imports `script.full_life_zero_access_page` / `script.page_origin_trace`, which are absent here; it is not runnable as-is in this checkout. Treat it as reference for the schema and parsing regexes.
- `outputs/README_fscache_residency.md` (Chinese) documents usage in more detail; example paths there are stale.
