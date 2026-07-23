#!/usr/bin/env python3
"""Bitmap vs. filemap page diff for tracked files (non-visual).

A tracked file's pages can show up in the ftrace DB through two independent
sources with different **scope**:
  - `bitmap_page_info` — periodic `tracing_mark_write ... bitmap` snapshots of
    which pages are currently mmap-mapped **in one traced process's address
    space** (decoded from a 64-bit bitmap into one row per set bit, see
    `parse_bitmap_to_offsets` in load_ftrace_to_db_file.py). A file only shows
    up here if that process actually mmap'd it.
  - `mm_filemap_add_to_page_cache` / `mm_filemap_delete_from_page_cache` — the
    page-cache lifecycle for that file, recorded **machine-wide** (every
    process, every access path: mmap, read(), readahead, ...).

Because bitmap is scoped to one process's mmap footprint and filemap is
whole-machine, it is *expected* that many files have filemap records but no
bitmap record at all (the file was never mmap'd by the traced process) — that
comparison is not meaningful. The meaningful comparison is restricted to
files the bitmap actually observed: for those files, do their bitmap-visible
pages line up with their filemap add/delete pages, and how much filemap
activity did the bitmap's periodic sampling miss?

Two modes:
  - Single file (`--ino`/`--dev` or `--file-like`): full page-level diff for
    one (dev, ino), listing every page with an add/delete record that no
    bitmap snapshot ever caught.
  - Batch (`--all-bitmap-files`): enumerate every (dev, ino) the bitmap ever
    observed and report the bitmap/filemap page-count comparison for each,
    ranked so the biggest gaps surface first. This is the report to use when
    asking "of the files bitmap actually saw, which ones does it disagree
    with filemap on?" across the whole DB.

Usage:
  python3 outputs/bitmap_filemap_diff.py --db ftrace.db --ino 0x1234 --dev 8:1
  python3 outputs/bitmap_filemap_diff.py --db ftrace.db --file-like '%libfoo.so%'
  python3 outputs/bitmap_filemap_diff.py --db ftrace.db --all-bitmap-files --csv report.csv
"""

import argparse
import collections
import json
import os
import sqlite3
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple

BITMAP_TABLE = "bitmap_page_info"
ADD_TABLE = "mm_filemap_add_to_page_cache"
DELETE_TABLE = "mm_filemap_delete_from_page_cache"
INODE_TABLE = "inode_mapping"

PAGE_SIZE = 4096
FileKey = Tuple[str, str]

# Per-ofs aggregate: [add_count, del_count, first_add_ts, last_add_ts,
#                      first_del_ts, last_del_ts, max_mmapcnt, last_pid_name]
A_ADDN, A_DELN, A_FIRST_ADD, A_LAST_ADD, A_FIRST_DEL, A_LAST_DEL, A_MMAPCNT, A_PID = range(8)


def connect(db_path: str, writable: bool = False) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise SystemExit(f"DB not found: {db_path}")
    mode = "rw" if writable else "ro"
    conn = sqlite3.connect(f"file:{db_path}?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    if not writable:
        conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute("PRAGMA cache_size = -1048576")
    try:
        conn.execute("PRAGMA mmap_size = 17179869184")
    except sqlite3.OperationalError:
        pass
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def require_tables(conn: sqlite3.Connection) -> None:
    missing = [t for t in (BITMAP_TABLE, ADD_TABLE, DELETE_TABLE) if not table_exists(conn, t)]
    if missing:
        raise SystemExit(f"Missing required table(s): {', '.join(missing)}")


def has_dev_ino_index(conn: sqlite3.Connection, table: str) -> bool:
    if not table_exists(conn, table):
        return False
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        cols = [r["name"] for r in conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()]
        if cols[:2] == ["dev", "ino"]:
            return True
    return False


def build_dev_ino_indexes(db_path: str) -> None:
    wconn = connect(db_path, writable=True)
    try:
        for table in (BITMAP_TABLE, ADD_TABLE, DELETE_TABLE):
            if has_dev_ino_index(wconn, table):
                continue
            print(f"Building (dev,ino) index on {table} (one-time)...", file=sys.stderr)
            wconn.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_dev_ino ON {table}(dev, ino)")
        wconn.commit()
    finally:
        wconn.close()


def normalize_ino(raw: str) -> str:
    raw = raw.strip()
    try:
        val = int(raw, 16) if raw.lower().startswith("0x") else int(raw)
    except ValueError:
        raise SystemExit(f"--ino: cannot parse '{raw}' as an integer")
    return f"0x{val:x}"


def resolve_target(conn: sqlite3.Connection, args: argparse.Namespace) -> Tuple[str, str, str]:
    """Returns (dev, ino, filename)."""
    if args.file_like:
        if not table_exists(conn, INODE_TABLE):
            raise SystemExit("--file-like requires the inode_mapping table.")
        rows = conn.execute(
            f"SELECT DISTINCT dev, ino, filename FROM {INODE_TABLE} WHERE filename LIKE ?",
            (args.file_like,),
        ).fetchall()
        if not rows:
            raise SystemExit(f"--file-like '{args.file_like}' matched no rows in inode_mapping.")
        if len(rows) > 1:
            print("Multiple files matched --file-like; narrow with --dev/--ino:", file=sys.stderr)
            for r in rows[:20]:
                print(f"  dev={r['dev']} ino={r['ino']} {r['filename']}", file=sys.stderr)
            raise SystemExit(1)
        row = rows[0]
        return str(row["dev"]), str(row["ino"]), str(row["filename"] or "")

    if not args.ino:
        raise SystemExit("Specify --ino (with --dev if ambiguous), --file-like, or --all-bitmap-files.")
    ino = normalize_ino(args.ino)

    dev = args.dev
    if not dev:
        devs = set()
        for table in (ADD_TABLE, DELETE_TABLE, BITMAP_TABLE):
            for r in conn.execute(f"SELECT DISTINCT dev FROM {table} WHERE ino=?", (ino,)):
                devs.add(str(r["dev"]))
        if len(devs) == 0:
            raise SystemExit(f"ino {ino} not found in add/delete/bitmap tables; check --ino.")
        if len(devs) > 1:
            raise SystemExit(
                f"ino {ino} appears on multiple devs {sorted(devs)}; disambiguate with --dev."
            )
        dev = next(iter(devs))

    filename = ""
    if table_exists(conn, INODE_TABLE):
        row = conn.execute(
            f"SELECT filename FROM {INODE_TABLE} WHERE dev=? AND ino=?", (dev, ino)
        ).fetchone()
        if row and row["filename"]:
            filename = str(row["filename"])
    return dev, ino, filename


# ==========================================
# Single-file mode
# ==========================================

def load_bitmap_offsets(
    conn: sqlite3.Connection, dev: str, ino: str,
    start: Optional[float], end: Optional[float],
) -> Tuple[set, int, Optional[float], Optional[float]]:
    clauses = ["dev = ?", "ino = ?"]
    params: List[object] = [dev, ino]
    if start is not None:
        clauses.append("timestamp >= ?"); params.append(start)
    if end is not None:
        clauses.append("timestamp <= ?"); params.append(end)
    where = " AND ".join(clauses)

    offsets: set = set()
    raw_rows = 0
    ts_min: Optional[float] = None
    ts_max: Optional[float] = None
    cur = conn.execute(f"SELECT page_ofs, timestamp FROM {BITMAP_TABLE} WHERE {where}", params)
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        for r in rows:
            raw_rows += 1
            offsets.add(int(r["page_ofs"]))
            ts = float(r["timestamp"])
            ts_min = ts if ts_min is None else min(ts_min, ts)
            ts_max = ts if ts_max is None else max(ts_max, ts)
    return offsets, raw_rows, ts_min, ts_max


def load_filemap_aggregate(
    conn: sqlite3.Connection, dev: str, ino: str,
    start: Optional[float], end: Optional[float],
) -> Dict[int, list]:
    clauses = ["dev = ?", "ino = ?"]
    params: List[object] = [dev, ino]
    if start is not None:
        clauses.append("timestamp >= ?"); params.append(start)
    if end is not None:
        clauses.append("timestamp <= ?"); params.append(end)
    where = " AND ".join(clauses)

    agg: Dict[int, list] = {}

    def get(ofs: int) -> list:
        st = agg.get(ofs)
        if st is None:
            st = [0, 0, None, None, None, None, 0, ""]
            agg[ofs] = st
        return st

    cur = conn.execute(
        f"SELECT ofs, timestamp, mmapcnt, pid_name FROM {ADD_TABLE} WHERE {where}", params
    )
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        for r in rows:
            ofs = int(r["ofs"])
            ts = float(r["timestamp"])
            st = get(ofs)
            st[A_ADDN] += 1
            st[A_FIRST_ADD] = ts if st[A_FIRST_ADD] is None else min(st[A_FIRST_ADD], ts)
            st[A_LAST_ADD] = ts if st[A_LAST_ADD] is None else max(st[A_LAST_ADD], ts)
            st[A_MMAPCNT] = max(st[A_MMAPCNT], int(r["mmapcnt"] or 0))
            st[A_PID] = str(r["pid_name"] or "")

    cur = conn.execute(
        f"SELECT ofs, timestamp, mmapcnt, pid_name FROM {DELETE_TABLE} WHERE {where}", params
    )
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        for r in rows:
            ofs = int(r["ofs"])
            ts = float(r["timestamp"])
            st = get(ofs)
            st[A_DELN] += 1
            st[A_FIRST_DEL] = ts if st[A_FIRST_DEL] is None else min(st[A_FIRST_DEL], ts)
            st[A_LAST_DEL] = ts if st[A_LAST_DEL] is None else max(st[A_LAST_DEL], ts)
            st[A_MMAPCNT] = max(st[A_MMAPCNT], int(r["mmapcnt"] or 0))
            st[A_PID] = str(r["pid_name"] or "")

    return agg


def build_missing_records(
    agg: Dict[int, list], missing_offsets: set, page_size: int
) -> List[dict]:
    records = []
    for ofs in missing_offsets:
        st = agg[ofs]
        records.append({
            "ofs": ofs,
            "pageIdx": ofs // page_size,
            "addCount": st[A_ADDN],
            "delCount": st[A_DELN],
            "firstAddTs": st[A_FIRST_ADD],
            "lastAddTs": st[A_LAST_ADD],
            "firstDelTs": st[A_FIRST_DEL],
            "lastDelTs": st[A_LAST_DEL],
            "stillResident": st[A_ADDN] > st[A_DELN],
            "maxMmapcnt": st[A_MMAPCNT],
            "lastPidName": st[A_PID],
        })
    return records


SORT_KEYS = {
    "ofs": lambda r: r["ofs"],
    "add_count": lambda r: (-r["addCount"], r["ofs"]),
    "first_add_ts": lambda r: (r["firstAddTs"] if r["firstAddTs"] is not None else 0.0, r["ofs"]),
}


def run_single(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    dev, ino, filename = resolve_target(conn, args)
    if not all(has_dev_ino_index(conn, t) for t in (BITMAP_TABLE, ADD_TABLE, DELETE_TABLE)):
        print("WARNING: no (dev,ino) index on one or more tables; this scans the full table. "
              "Re-run with --build-indices once for repeated use.", file=sys.stderr)

    print(f"Target: dev={dev} ino={ino}" + (f"  {filename}" if filename else ""), file=sys.stderr)

    bitmap_offsets, bitmap_raw_rows, bmin, bmax = load_bitmap_offsets(conn, dev, ino, args.start, args.end)
    agg = load_filemap_aggregate(conn, dev, ino, args.start, args.end)
    filemap_offsets = set(agg.keys())

    missing = filemap_offsets - bitmap_offsets   # filemap add/delete seen, bitmap never saw
    bitmap_only = bitmap_offsets - filemap_offsets
    both = filemap_offsets & bitmap_offsets

    missing_records = build_missing_records(agg, missing, args.page_size)
    missing_records.sort(key=SORT_KEYS[args.sort_by])

    n_filemap = len(filemap_offsets)
    n_missing = len(missing_records)
    frac = (n_missing / n_filemap) if n_filemap else 0.0

    result = {
        "params": {
            "dev": dev, "ino": ino, "filename": filename,
            "start": args.start, "end": args.end,
            "pageSize": args.page_size,
        },
        "counts": {
            "bitmapDistinctPages": len(bitmap_offsets),
            "bitmapRawRows": bitmap_raw_rows,
            "filemapDistinctPages": n_filemap,
            "pagesInBoth": len(both),
            "filemapOnlyPages": n_missing,
            "filemapOnlyFraction": round(frac, 6),
            "bitmapOnlyPages": len(bitmap_only),
        },
        "bitmapObservationWindow": {"start": bmin, "end": bmax},
        "missingPages": missing_records,
    }

    c = result["counts"]
    print("", file=sys.stderr)
    print(f"Bitmap-observed pages   : {c['bitmapDistinctPages']:,} distinct "
          f"({c['bitmapRawRows']:,} raw snapshot rows)", file=sys.stderr)
    print(f"Filemap add/delete pages: {c['filemapDistinctPages']:,} distinct", file=sys.stderr)
    print(f"In both                 : {c['pagesInBoth']:,}", file=sys.stderr)
    print(f"Filemap-only (missing from bitmap): {c['filemapOnlyPages']:,} "
          f"({c['filemapOnlyFraction']*100:.1f}% of filemap pages)", file=sys.stderr)
    print(f"Bitmap-only (no add/delete record): {c['bitmapOnlyPages']:,}", file=sys.stderr)

    if missing_records:
        print("", file=sys.stderr)
        print(f"Top {min(args.top, len(missing_records))} filemap-only pages (sort={args.sort_by}):", file=sys.stderr)
        header = f"{'ofs':>12} {'pageIdx':>9} {'add':>4} {'del':>4} {'firstAdd':>12} {'lastDel':>12} {'resident':>8} {'pidName'}"
        print(header, file=sys.stderr)
        for r in missing_records[: args.top]:
            print(
                f"{r['ofs']:>12} {r['pageIdx']:>9} {r['addCount']:>4} {r['delCount']:>4} "
                f"{('%.6f' % r['firstAddTs']) if r['firstAddTs'] is not None else '-':>12} "
                f"{('%.6f' % r['lastDelTs']) if r['lastDelTs'] is not None else '-':>12} "
                f"{str(r['stillResident']):>8} {r['lastPidName']}",
                file=sys.stderr,
            )

    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"Wrote {os.path.abspath(args.json)}", file=sys.stderr)

    if args.csv:
        with open(args.csv, "w") as f:
            f.write("ofs,page_idx,add_count,del_count,first_add_ts,last_add_ts,"
                     "first_del_ts,last_del_ts,still_resident,max_mmapcnt,last_pid_name\n")
            for r in missing_records:
                f.write(
                    f"{r['ofs']},{r['pageIdx']},{r['addCount']},{r['delCount']},"
                    f"{r['firstAddTs'] if r['firstAddTs'] is not None else ''},"
                    f"{r['lastAddTs'] if r['lastAddTs'] is not None else ''},"
                    f"{r['firstDelTs'] if r['firstDelTs'] is not None else ''},"
                    f"{r['lastDelTs'] if r['lastDelTs'] is not None else ''},"
                    f"{r['stillResident']},{r['maxMmapcnt']},\"{r['lastPidName']}\"\n"
                )
        print(f"Wrote {os.path.abspath(args.csv)}", file=sys.stderr)

    return 0


# ==========================================
# Batch mode: every (dev,ino) the bitmap ever observed
# ==========================================

def load_bitmap_all(
    conn: sqlite3.Connection, start: Optional[float], end: Optional[float],
) -> Tuple[Dict[FileKey, Set[int]], Dict[FileKey, int]]:
    clauses: List[str] = []
    params: List[object] = []
    if start is not None:
        clauses.append("timestamp >= ?"); params.append(start)
    if end is not None:
        clauses.append("timestamp <= ?"); params.append(end)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sets: Dict[FileKey, Set[int]] = {}
    raw_counts: Dict[FileKey, int] = collections.defaultdict(int)
    cur = conn.execute(f"SELECT dev, ino, page_ofs FROM {BITMAP_TABLE} {where}", params)
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        for r in rows:
            key = (str(r["dev"]), str(r["ino"]))
            raw_counts[key] += 1
            sets.setdefault(key, set()).add(int(r["page_ofs"]))
    return sets, dict(raw_counts)


def load_filemap_offsets_for_keys(
    conn: sqlite3.Connection, keys: Set[FileKey],
    start: Optional[float], end: Optional[float],
) -> Dict[FileKey, Set[int]]:
    """Single full-table-scan pass over add+delete, keeping only inodes in `keys`.

    Mirrors the --file-like key-set pattern used in fscache_residency.py
    (Python membership test up front, not one SQL query per inode) so batch
    mode stays a fixed number of table scans regardless of how many bitmap
    files there are."""
    clauses: List[str] = []
    params: List[object] = []
    if start is not None:
        clauses.append("timestamp >= ?"); params.append(start)
    if end is not None:
        clauses.append("timestamp <= ?"); params.append(end)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    result: Dict[FileKey, Set[int]] = {}
    for table in (ADD_TABLE, DELETE_TABLE):
        cur = conn.execute(f"SELECT dev, ino, ofs FROM {table} {where}", params)
        while True:
            rows = cur.fetchmany(50000)
            if not rows:
                break
            for r in rows:
                key = (str(r["dev"]), str(r["ino"]))
                if key not in keys:
                    continue
                result.setdefault(key, set()).add(int(r["ofs"]))
    return result


def build_batch_report(
    bitmap_sets: Dict[FileKey, Set[int]],
    bitmap_raw_counts: Dict[FileKey, int],
    filemap_sets: Dict[FileKey, Set[int]],
    filenames: Dict[FileKey, str],
) -> List[dict]:
    rows = []
    for key, bset in bitmap_sets.items():
        dev, ino = key
        fset = filemap_sets.get(key, set())
        both = bset & fset
        fm_only = len(fset - bset)
        bm_only = len(bset - fset)
        rows.append({
            "dev": dev, "ino": ino, "filename": filenames.get(key, ""),
            "bitmapPages": len(bset),
            "bitmapRawRows": bitmap_raw_counts.get(key, 0),
            "filemapPages": len(fset),
            "bothPages": len(both),
            "filemapOnlyPages": fm_only,
            "filemapOnlyFraction": round(fm_only / len(fset), 6) if fset else 0.0,
            "bitmapOnlyPages": bm_only,
            "bitmapOnlyFraction": round(bm_only / len(bset), 6) if bset else 0.0,
        })
    return rows


BATCH_SORT_KEYS = {
    "both": lambda r: (-r["bothPages"], r["dev"], r["ino"]),
    "filemap_only": lambda r: (-r["filemapOnlyPages"], r["dev"], r["ino"]),
    "bitmap_only": lambda r: (-r["bitmapOnlyPages"], r["dev"], r["ino"]),
    "filemap_pages": lambda r: (-r["filemapPages"], r["dev"], r["ino"]),
    "bitmap_pages": lambda r: (-r["bitmapPages"], r["dev"], r["ino"]),
}


def run_batch(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.ino or args.file_like:
        raise SystemExit("--all-bitmap-files cannot be combined with --ino/--file-like.")

    print("Batch mode: enumerating every (dev,ino) observed by the bitmap (one process's "
          "mmap footprint) and comparing against whole-machine filemap add/delete pages "
          "for the same files only.", file=sys.stderr)

    bitmap_sets, bitmap_raw_counts = load_bitmap_all(conn, args.start, args.end)
    if not bitmap_sets:
        print("No bitmap_page_info rows in the given window.", file=sys.stderr)
        return 0
    keys = set(bitmap_sets.keys())
    print(f"Bitmap observed {len(keys):,} distinct (dev,ino) files.", file=sys.stderr)

    filenames: Dict[FileKey, str] = {}
    if table_exists(conn, INODE_TABLE):
        for r in conn.execute(f"SELECT dev, ino, filename FROM {INODE_TABLE}"):
            key = (str(r["dev"]), str(r["ino"]))
            if key in keys and r["filename"]:
                filenames[key] = str(r["filename"])

    filemap_sets = load_filemap_offsets_for_keys(conn, keys, args.start, args.end)

    rows = build_batch_report(bitmap_sets, bitmap_raw_counts, filemap_sets, filenames)
    rows.sort(key=BATCH_SORT_KEYS[args.rank_by])

    totals = {
        "bitmapFiles": len(rows),
        "filesWithIntersection": sum(1 for r in rows if r["bothPages"] > 0),
        "filesWithZeroIntersection": sum(1 for r in rows if r["bothPages"] == 0),
        "bitmapPages": sum(r["bitmapPages"] for r in rows),
        "filemapPages": sum(r["filemapPages"] for r in rows),
        "bothPages": sum(r["bothPages"] for r in rows),
        "filemapOnlyPages": sum(r["filemapOnlyPages"] for r in rows),
        "bitmapOnlyPages": sum(r["bitmapOnlyPages"] for r in rows),
    }

    result = {
        "params": {"start": args.start, "end": args.end, "pageSize": args.page_size, "rankBy": args.rank_by},
        "totals": totals,
        "files": rows,
    }

    print("", file=sys.stderr)
    print(f"Bitmap-observed files             : {totals['bitmapFiles']:,}", file=sys.stderr)
    print(f"  with >=1 page also in filemap   : {totals['filesWithIntersection']:,}", file=sys.stderr)
    print(f"  with zero filemap intersection  : {totals['filesWithZeroIntersection']:,}", file=sys.stderr)
    print(f"Total bitmap pages (these files)  : {totals['bitmapPages']:,}", file=sys.stderr)
    print(f"Total filemap pages (these files) : {totals['filemapPages']:,}", file=sys.stderr)
    print(f"In both                           : {totals['bothPages']:,}", file=sys.stderr)
    print(f"Filemap-only (bitmap missed)      : {totals['filemapOnlyPages']:,}", file=sys.stderr)
    print(f"Bitmap-only (no filemap record)   : {totals['bitmapOnlyPages']:,}", file=sys.stderr)

    if rows:
        print("", file=sys.stderr)
        print(f"Top {min(args.top, len(rows))} files (rank-by={args.rank_by}):", file=sys.stderr)
        header = (f"{'dev':>8} {'ino':>12} {'bitmapPg':>9} {'filemapPg':>10} "
                  f"{'both':>7} {'fmOnly':>7} {'bmOnly':>7}  filename")
        print(header, file=sys.stderr)
        for r in rows[: args.top]:
            print(
                f"{r['dev']:>8} {r['ino']:>12} {r['bitmapPages']:>9} {r['filemapPages']:>10} "
                f"{r['bothPages']:>7} {r['filemapOnlyPages']:>7} {r['bitmapOnlyPages']:>7}  {r['filename']}",
                file=sys.stderr,
            )

    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"Wrote {os.path.abspath(args.json)}", file=sys.stderr)

    if args.csv:
        with open(args.csv, "w") as f:
            f.write("dev,ino,filename,bitmap_pages,bitmap_raw_rows,filemap_pages,both_pages,"
                     "filemap_only_pages,filemap_only_fraction,bitmap_only_pages,bitmap_only_fraction\n")
            for r in rows:
                fn = str(r["filename"]).replace('"', '""')
                f.write(
                    f"{r['dev']},{r['ino']},\"{fn}\",{r['bitmapPages']},{r['bitmapRawRows']},"
                    f"{r['filemapPages']},{r['bothPages']},{r['filemapOnlyPages']},"
                    f"{r['filemapOnlyFraction']},{r['bitmapOnlyPages']},{r['bitmapOnlyFraction']}\n"
                )
        print(f"Wrote {os.path.abspath(args.csv)}", file=sys.stderr)

    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Diff mmap-bitmap-observed pages vs. filemap add/delete pages, per file or batch."
    )
    p.add_argument("--db", required=True, help="Path to the ftrace SQLite DB.")
    p.add_argument("--ino", help="Target inode (hex '0x...' or decimal). Requires --dev if ambiguous.")
    p.add_argument("--dev", help="Target dev ('major:minor'), used with --ino.")
    p.add_argument("--file-like", help="SQL LIKE pattern on inode_mapping.filename to resolve dev/ino.")
    p.add_argument("--all-bitmap-files", action="store_true",
                   help="Batch mode: report every (dev,ino) the bitmap ever observed, ranked by "
                        "gap vs. its filemap add/delete pages. Mutually exclusive with --ino/--file-like.")
    p.add_argument("--rank-by", choices=sorted(BATCH_SORT_KEYS), default="filemap_only",
                   help="Batch mode ranking (default: filemap_only, i.e. biggest bitmap-missed gaps first).")
    p.add_argument("--start", type=float, help="Start timestamp filter.")
    p.add_argument("--end", type=float, help="End timestamp filter.")
    p.add_argument("--page-size", type=int, default=PAGE_SIZE, help="Page size in bytes (default 4096).")
    p.add_argument("--top", type=int, default=50, help="How many rows to print to stderr (default 50).")
    p.add_argument("--sort-by", choices=sorted(SORT_KEYS), default="ofs",
                   help="Single-file mode: sort order for the printed/exported missing-page list.")
    p.add_argument("--json", help="Write full result JSON to this path.")
    p.add_argument("--csv", help="Write the missing-page list (single-file) or file report (batch) as CSV.")
    p.add_argument("--build-indices", action="store_true",
                   help="Create missing (dev,ino) indexes on bitmap/add/delete tables (one-time write).")
    return p.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    if args.build_indices:
        build_dev_ino_indexes(args.db)

    conn = connect(args.db)
    require_tables(conn)

    if args.all_bitmap_files:
        return run_batch(conn, args)
    return run_single(conn, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
