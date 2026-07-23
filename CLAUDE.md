# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

Reconstruct and visualize Linux **file page-cache residency lifecycles** from ftrace data. The pipeline has two independent halves:

1. **Ingest** (`load_ftrace_to_db_file.py`, repo root): parses raw ftrace text logs (plus `smaps`, `inode.txt`, ark disassembly, step-info) into a SQLite DB via regexes, one table per event type.
2. **Visualize** (`outputs/fscache_residency.py`): reads that DB read-only, reconstructs residency intervals, and emits a **standalone self-contained HTML viewer** (vanilla JS + `<canvas>`, no external/CDN dependencies — data is embedded as inline JSON).
3. **Analyze** (`outputs/cold_page_stats.py`): non-visual stats tool. Streams add/delete/access(+`label`>1) events in timestamp order, reconstructs each `(dev,ino,ofs)` add→delete interval, flags **cold pages** (≤1 access), and reports cold **spacetime** (page-seconds; 0-access = delete−add, 1-access = delete−access) and average cold footprint (spacetime ÷ test window). Same streaming/index/pragma performance model as the visualizer; `--build-indices`, `--no-label-access`, `--top`, `--json`, `--csv`.
4. **Diff** (`outputs/bitmap_filemap_diff.py`): non-visual tool comparing two independent page sources for a file: `bitmap_page_info` (periodic mmap-bitmap snapshots, scoped to **one traced process's address space**) vs. `mm_filemap_add_to_page_cache`/`_delete_from_page_cache` (page-cache lifecycle, recorded **whole-machine**). Because the scopes differ, only files the bitmap actually observed are a fair comparison. Single-file mode (`--ino`/`--dev` or `--file-like`) lists every page with a filemap add/delete record the bitmap never caught. Batch mode (`--all-bitmap-files`) enumerates every `(dev,ino)` the bitmap ever saw and ranks them by bitmap/filemap page-count gaps (`--rank-by`); a single full-scan pass over add/delete (and `mm_filemap_access_history`, if present) filtered by a Python key-set (same pattern as `--file-like` in the visualizer), not one query per file. Every row also carries `addProcesses`/`accessProcesses` (`pid_name`s from add and access events). Filenames resolve from `inode_mapping` by **ino alone** (its `dev` doesn't reliably match the ftrace-reported dev); an ino spanning more than one dev in the report is left unresolved rather than guessed. `--build-indices`, `--json`, `--csv`, `--sqlite-out PATH [--sqlite-table NAME]` (writes/overwrites a table in a SQLite DB).
5. **Analyze** (`outputs/single_process_page_stats.py`): non-visual stats tool. Computes the deduped **population** of `(dev,ino,ofs)` pages seen in `mm_filemap_add_to_page_cache` ("loaded") ∪ `bitmap_page_info` (mmap-observed), then reports how many of those are **single-owner and unmapped**: `mmapcnt==0` across all their add/delete/access rows *and* touched by exactly one process, plus that count's % of the population. "Touched by" is a **priority cascade, not a union**: if the page has any `mm_filemap_access_history` rows, its owner set is access's `pid_name`s alone; else add's; else (add- and access-less) delete's. The bitmap snapshot's own `pid_name` is never counted — it's the shell command (e.g. `cat`) that dumped the bitmap, not a real accessor. A page seen only in bitmap (no add/delete/access rows at all) has no mmapcnt data and is excluded from the mmapcnt==0 cohort rather than defaulted. Four independent full-table scans (no timestamp-merge needed, no per-key state machine); `--no-access` (falls through to add, then delete), `--build-indices`, `--top`, `--json`, `--csv`. **Memory-tuned for 10s-of-GB DBs**: the aggregate is nested `{"dev|ino" -> {ofs -> state}}` (interned file key, matching `cold_page_stats.py`'s `make_file_key`) rather than a flat `(dev,ino,ofs)`-tuple-keyed dict, each page's state packs 6 booleans into one int and tracks each event source's owning pid as a single nullable interned string instead of a `set()`, and the whole aggregate is `del`eted right after the one summary pass extracts what reporting needs (measured ~5x smaller than the naive per-page-`set()` version — see the 2026-07-23 devlog for the benchmark).

The halves are decoupled by the SQLite schema. Most work happens in the visualizer; the ingest script depends on data paths and a `script/` package that are **not checked into this repo**. `devlog/` holds dated development-log entries describing feature goals/plans and what was implemented.

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
- `--file-like` / `--pid-like` — SQL `LIKE` include-filters on `inode_mapping.filename` / `pid_name`.
- `--exclude-file GLOB` — drop inodes whose `inode_mapping.filename` matches a glob (`*`/`?`), e.g. `--exclude-file /data/log/*`. Repeatable. Resolved once to a set of `dev|ino` keys (`resolve_excluded_file_keys`) and applied as a Python membership test in `iter_events`/`load_candidate_pages`; stashed on `args.exclude_keys`.
- `--no-access` — reconstruct residency from add/delete only; skip `access` events (which reassign a page's owning `pid_name`).
- `--max-lanes` / `--max-heatmap-files` / `--max-groups` / `--max-coldmap-files` — cap how much detail survives into each chart.
- `--bucket-seconds` / `--target-points` — aggregate-curve resolution (auto-targets ~260 points).
- `--build-indices` — create missing `timestamp` indexes on the event tables (one-time write to the DB). **Run this once on large DBs.**

### Performance model (matters for multi-GB DBs / tens of millions of events)
The visualizer streams the merged add/delete/access log in timestamp order, twice (peaks pass + reconstruct pass). How that stream is produced depends on indexes:
- **With a `timestamp`-leading index on each event table** (`has_all_timestamp_indexes`): `iter_events` runs one index-ordered cursor per table and merges them with `heapq.merge` — no sort, bounded memory, `SCAN ... USING INDEX`.
- **Without** those indexes: it falls back to a single `UNION ALL ... ORDER BY timestamp`, which makes SQLite build a temp b-tree (`USE TEMP B-TREE FOR ORDER BY`) over *every* event. `connect()` sets `temp_store=FILE` so this spills to disk instead of OOMing, but it is slow — the visualizer prints a WARNING recommending `--build-indices`.

The ingest script (`load_ftrace_to_db_file.py`) does **not** create these indexes (its `build_indices` is commented out), so freshly-built real DBs need `--build-indices` once. Other perf-relevant choices: `--file-like` is resolved to a set of `dev|ino` keys up front (Python membership test, not a per-row SQL `EXISTS`); per-resident-page state is a 5-element list, not a dict, to bound memory when millions of pages are resident at once.

## Architecture of the visualizer

The core model: a page is identified by `(dev, ino, ofs)`. Events drive a state machine over resident intervals:
- `mm_filemap_add_to_page_cache` → opens a resident interval.
- `mm_filemap_delete_from_page_cache` → closes it.
- `mm_filemap_access_history` → does **not** change residency, but reassigns the interval's current owning `pid_name` (this is what powers the "color by pid_name" mode vs. "color by file" mode).

`main()` runs **two passes over the event stream** (events are merged from the add/delete/access tables, ordered by timestamp, via `iter_events`):
- **Pass 1 — `pass_for_peaks`**: finds peak-residency moments and selects the top-N files, `pid_name`s, and file+pid groups to color, plus heatmap files.
- **Pass 2 — `reconstruct`**: rebuilds the aggregate residency curve, per-page lifecycle lanes, and the heatmap, keyed to the groups chosen in pass 1.

Pass 2 also computes the **whole-machine cold/hot heatmap**, carried in the `heatmap` payload alongside the residency values. For each retained inode and time bucket it records `values` (resident pages, the existing residency heatmap) and `accessed` (distinct resident pages of that inode that got an access within the bucket, tracked via per-bucket `bucket_accessed` sets, capped at the resident count). The viewer renders it as a second time×inode heatmap where each inode's band **thickness = resident pages** drawn on a shared `px/page` scale (the global peak maps to a fixed pixel band, so row heights vary and thickness is a true page count, not per-row normalized) and **color = accessed/resident ratio** on a vibrant selectable palette (Turbo/Jet/Inferno/Viridis/Icefire) with an adaptive/absolute color-range toggle. Inodes holding many resident pages that are rarely accessed show up as thick, cold-colored bands.

The result dict feeds `write_outputs`, which substitutes it into `HTML_TEMPLATE` (a big raw-string, `HTML_TEMPLATE = r"""..."""`) at the `__DATA__` placeholder. **The viewer is one Python string containing HTML+CSS+JS** — edit the template in place; there is no separate front-end build.

**Anomaly counters** (`delete_without_active_add`, `access_without_active_add`, `duplicate_add_closed_previous`) are tallied during reconstruction and surfaced in the output; nonzero values usually mean the trace window is truncated or events were dropped, not a code bug.

## SQLite schema (contract between the two halves)

Tables are created/dropped in `init_database` (`load_ftrace_to_db_file.py:81`). The visualizer only requires `mm_filemap_add_to_page_cache` and `mm_filemap_delete_from_page_cache` (and `mm_filemap_access_history` unless `--no-access`); `inode_mapping` (`dev,ino → filename,size`) and `timestep` (business-step reference lines) are optional enrichment. Event tables carry `(dev, ino, page, pfn, ofs, mmapcnt, flags, timestamp, pid, pid_name)`.

`bitmap_page_info` (`dev, ino, base_ofs, page_ofs, timestamp, pid, pid_name`, no `page`/`pfn`/`mmapcnt`/`flags`) is a separate mmap-bitmap source, decoded from a 64-bit-per-line `tracing_mark_write ... bitmap` tracepoint (`parse_bitmap_to_offsets`) into one row per resident page (`page_ofs`, byte offset — same scale as `ofs` in the filemap tables). It is only consumed by `outputs/bitmap_filemap_diff.py`; it's scoped to whichever single process was traced for mmap, not whole-machine like the filemap tables.

When adding a new event source, add its regex + table in the ingest script **and** teach `fscache_residency.py` to read it — the schema is the only coupling point.

## Notes / gotchas
- `outputs/` contains committed sample artifacts (`sample_fscache_residency.*`, `top_apps_fscache_demo.*`) — regenerating them overwrites those files.
- `load_ftrace_to_db_file.py` `main()` has hardcoded Windows-style input paths and imports `script.full_life_zero_access_page` / `script.page_origin_trace`, which are absent here; it is not runnable as-is in this checkout. Treat it as reference for the schema and parsing regexes.
- `outputs/README_fscache_residency.md` (Chinese) documents usage in more detail; example paths there are stale.
