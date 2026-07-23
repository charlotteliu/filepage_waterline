#!/usr/bin/env python3
"""Single-process, unmapped page-population stats over the ftrace DB (non-visual).

Two numbers:

1. **Population**: the count of distinct `(dev,ino,ofs)` file pages that
   appear either in `mm_filemap_add_to_page_cache` ("loaded" into the page
   cache) or in `bitmap_page_info` (observed mmap-mapped in the traced
   process's bitmap snapshots), deduplicated (union of the two sources).
2. **Single-owner unmapped pages**: of that population, how many have
   `mmapcnt == 0` (never observed with an active mmap reference) *and* were
   ever touched by exactly one process — plus that count's percentage of
   the population from (1).

Field semantics (see CLAUDE.md for the full schema):
  - `mmapcnt` only exists on add/delete/access-history rows (bitmap rows
    don't carry it). A page's mmapcnt is the max seen across all its
    add/delete/access rows. A page observed *only* in bitmap has no
    mmapcnt data — since bitmap membership itself means "currently
    mmap-mapped", such a page is excluded from the mmapcnt==0 cohort
    rather than defaulted to 0.
  - "Touched by a process" = the page's `pid_name` from add, delete,
    access-history, *and* the bitmap snapshot's own pid_name (the process
    whose address space that bitmap belongs to) — `--no-access` /
    `--no-bitmap-pid` narrow this if you want a stricter/looser definition.

Usage:
  python3 outputs/single_process_page_stats.py --db ftrace.db \
    --json single_owner_stats.json --csv single_owner_pages.csv
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
ACCESS_TABLE = "mm_filemap_access_history"
INODE_TABLE = "inode_mapping"

PAGE_SIZE = 4096
PageKey = Tuple[str, str, int]

# Per-page aggregate: [in_add, in_bitmap, max_mmapcnt, has_mmapcnt_data, pid_names]
P_IN_ADD, P_IN_BITMAP, P_MMAPCNT, P_HAS_MMAPCNT, P_PIDS = range(5)


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
    missing = [t for t in (ADD_TABLE, BITMAP_TABLE) if not table_exists(conn, t)]
    if missing:
        raise SystemExit(f"Missing required table(s): {', '.join(missing)}")


def has_timestamp_index(conn: sqlite3.Connection, table: str) -> bool:
    if not table_exists(conn, table):
        return False
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        cols = conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
        if cols and cols[0]["name"] == "timestamp":
            return True
    return False


def used_tables(conn: sqlite3.Connection, use_access: bool) -> List[str]:
    tables = [ADD_TABLE, DELETE_TABLE, BITMAP_TABLE]
    if use_access and table_exists(conn, ACCESS_TABLE):
        tables.append(ACCESS_TABLE)
    return [t for t in tables if table_exists(conn, t)]


def build_timestamp_indexes(db_path: str, use_access: bool) -> None:
    wconn = connect(db_path, writable=True)
    try:
        for table in used_tables(wconn, use_access):
            if has_timestamp_index(wconn, table):
                continue
            print(f"Building timestamp index on {table} (one-time)...", file=sys.stderr)
            wconn.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_ts ON {table}(timestamp)")
        wconn.commit()
    finally:
        wconn.close()


def resolve_filenames_by_ino(conn: sqlite3.Connection, keys: Set[Tuple[str, str]]) -> Dict[Tuple[str, str], str]:
    """Map (dev,ino) -> filename via inode_mapping, matched on ino alone
    (its dev column doesn't reliably agree with the ftrace-reported dev). An
    ino spanning more than one distinct dev within `keys` is ambiguous and
    left unresolved for all of them."""
    if not table_exists(conn, INODE_TABLE) or not keys:
        return {}
    ino_devs: Dict[str, Set[str]] = collections.defaultdict(set)
    for dev, ino in keys:
        ino_devs[ino].add(dev)
    inos = set(ino_devs)

    fn_by_ino: Dict[str, str] = {}
    for r in conn.execute(f"SELECT ino, filename FROM {INODE_TABLE}"):
        ino = str(r["ino"])
        if ino in inos and r["filename"]:
            fn_by_ino[ino] = str(r["filename"])

    result: Dict[Tuple[str, str], str] = {}
    for dev, ino in keys:
        if ino in fn_by_ino and len(ino_devs[ino]) == 1:
            result[(dev, ino)] = fn_by_ino[ino]
    return result


def _time_where(start: Optional[float], end: Optional[float]) -> Tuple[str, List[object]]:
    clauses: List[str] = []
    params: List[object] = []
    if start is not None:
        clauses.append("timestamp >= ?"); params.append(start)
    if end is not None:
        clauses.append("timestamp <= ?"); params.append(end)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def collect(
    conn: sqlite3.Connection,
    start: Optional[float], end: Optional[float],
    use_access: bool, use_bitmap_pid: bool,
) -> Tuple[Dict[PageKey, list], collections.Counter]:
    agg: Dict[PageKey, list] = {}
    anomalies: collections.Counter = collections.Counter()

    def get(key: PageKey) -> list:
        st = agg.get(key)
        if st is None:
            st = [False, False, 0, False, set()]
            agg[key] = st
        return st

    where, params = _time_where(start, end)

    # bitmap first: population membership + (optionally) the traced process's pid_name.
    cur = conn.execute(f"SELECT dev, ino, page_ofs, pid_name FROM {BITMAP_TABLE} {where}", params)
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        for r in rows:
            key = (str(r["dev"]), str(r["ino"]), int(r["page_ofs"]))
            st = get(key)
            st[P_IN_BITMAP] = True
            if use_bitmap_pid:
                pid_name = str(r["pid_name"] or "")
                if pid_name:
                    st[P_PIDS].add(pid_name)

    # add: population membership + mmapcnt + pid.
    cur = conn.execute(f"SELECT dev, ino, ofs, mmapcnt, pid_name FROM {ADD_TABLE} {where}", params)
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        for r in rows:
            key = (str(r["dev"]), str(r["ino"]), int(r["ofs"]))
            st = get(key)
            st[P_IN_ADD] = True
            st[P_HAS_MMAPCNT] = True
            st[P_MMAPCNT] = max(st[P_MMAPCNT], int(r["mmapcnt"] or 0))
            pid_name = str(r["pid_name"] or "")
            if pid_name:
                st[P_PIDS].add(pid_name)

    # delete: enrich mmapcnt/pid only (does not, by itself, define population).
    if table_exists(conn, DELETE_TABLE):
        cur = conn.execute(f"SELECT dev, ino, ofs, mmapcnt, pid_name FROM {DELETE_TABLE} {where}", params)
        while True:
            rows = cur.fetchmany(50000)
            if not rows:
                break
            for r in rows:
                key = (str(r["dev"]), str(r["ino"]), int(r["ofs"]))
                st = agg.get(key)
                if st is None:
                    anomalies["delete_without_add_or_bitmap"] += 1
                    st = get(key)  # keep the data; excluded from population at aggregation time
                st[P_HAS_MMAPCNT] = True
                st[P_MMAPCNT] = max(st[P_MMAPCNT], int(r["mmapcnt"] or 0))
                pid_name = str(r["pid_name"] or "")
                if pid_name:
                    st[P_PIDS].add(pid_name)

    # access_history: enrich mmapcnt/pid only, same as delete.
    if use_access and table_exists(conn, ACCESS_TABLE):
        cur = conn.execute(f"SELECT dev, ino, ofs, mmapcnt, pid_name FROM {ACCESS_TABLE} {where}", params)
        while True:
            rows = cur.fetchmany(50000)
            if not rows:
                break
            for r in rows:
                key = (str(r["dev"]), str(r["ino"]), int(r["ofs"]))
                st = agg.get(key)
                if st is None:
                    anomalies["access_without_add_or_bitmap"] += 1
                    st = get(key)
                st[P_HAS_MMAPCNT] = True
                st[P_MMAPCNT] = max(st[P_MMAPCNT], int(r["mmapcnt"] or 0))
                pid_name = str(r["pid_name"] or "")
                if pid_name:
                    st[P_PIDS].add(pid_name)

    return agg, anomalies


def analyze(agg: Dict[PageKey, list]) -> dict:
    population = [k for k, st in agg.items() if st[P_IN_ADD] or st[P_IN_BITMAP]]
    pop_count = len(population)

    in_add_only = in_bitmap_only = in_both = 0
    no_mmapcnt_data = 0
    qualifying: List[PageKey] = []

    for key in population:
        st = agg[key]
        if st[P_IN_ADD] and st[P_IN_BITMAP]:
            in_both += 1
        elif st[P_IN_ADD]:
            in_add_only += 1
        else:
            in_bitmap_only += 1

        if not st[P_HAS_MMAPCNT]:
            no_mmapcnt_data += 1
            continue  # bitmap-only page: no mmapcnt data -> can't be "mmapcnt==0"

        if st[P_MMAPCNT] == 0 and len(st[P_PIDS]) == 1:
            qualifying.append(key)

    qual_count = len(qualifying)
    pct = (qual_count / pop_count * 100.0) if pop_count else 0.0

    return {
        "population": {
            "totalPages": pop_count,
            "inAddOnly": in_add_only,
            "inBitmapOnly": in_bitmap_only,
            "inBoth": in_both,
            "noMmapcntData": no_mmapcnt_data,
        },
        "singleOwnerUnmapped": {
            "pageCount": qual_count,
            "percentOfPopulation": round(pct, 4),
        },
        "_qualifying_keys": qualifying,
    }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Population of loaded+bitmap pages, and the share that are "
                    "mmapcnt==0 and touched by exactly one process."
    )
    p.add_argument("--db", required=True, help="Path to the ftrace SQLite DB.")
    p.add_argument("--start", type=float, help="Start timestamp filter.")
    p.add_argument("--end", type=float, help="End timestamp filter.")
    p.add_argument("--page-size", type=int, default=PAGE_SIZE, help="Page size in bytes (default 4096).")
    p.add_argument("--no-access", dest="use_access", action="store_false",
                   help="Don't use mm_filemap_access_history for mmapcnt/pid enrichment.")
    p.set_defaults(use_access=True)
    p.add_argument("--no-bitmap-pid", dest="use_bitmap_pid", action="store_false",
                   help="Don't count a bitmap snapshot's pid_name as a touching process.")
    p.set_defaults(use_bitmap_pid=True)
    p.add_argument("--top", type=int, default=25, help="How many qualifying pages to print (default 25).")
    p.add_argument("--json", help="Write full result JSON to this path.")
    p.add_argument("--csv", help="Write the qualifying-page list as CSV to this path.")
    p.add_argument("--build-indices", action="store_true",
                   help="Create missing timestamp indexes (writes to DB, one-time; only helps --start/--end).")
    return p.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    if args.build_indices:
        build_timestamp_indexes(args.db, args.use_access)

    conn = connect(args.db)
    require_tables(conn)

    print("Scanning add/delete/bitmap" + ("/access" if args.use_access else "") + " tables...", file=sys.stderr)
    agg, anomalies = collect(conn, args.start, args.end, args.use_access, args.use_bitmap_pid)
    stats = analyze(agg)
    qualifying_keys = stats.pop("_qualifying_keys")

    pop = stats["population"]
    own = stats["singleOwnerUnmapped"]
    print("", file=sys.stderr)
    print(f"Population (loaded + bitmap, deduped): {pop['totalPages']:,} pages", file=sys.stderr)
    print(f"  add-only: {pop['inAddOnly']:,}  bitmap-only: {pop['inBitmapOnly']:,}  "
          f"both: {pop['inBoth']:,}  (no mmapcnt data: {pop['noMmapcntData']:,})", file=sys.stderr)
    print(f"Single-owner, mmapcnt==0 pages: {own['pageCount']:,} "
          f"({own['percentOfPopulation']:.2f}% of population)", file=sys.stderr)
    if anomalies:
        print(f"Anomalies: {dict(anomalies)}", file=sys.stderr)

    filenames: Dict[Tuple[str, str], str] = {}
    if table_exists(conn, INODE_TABLE) and qualifying_keys:
        filenames = resolve_filenames_by_ino(conn, {(dev, ino) for dev, ino, _ofs in qualifying_keys})

    def qual_row(key: PageKey) -> dict:
        dev, ino, ofs = key
        st = agg[key]
        pid = next(iter(st[P_PIDS]), "")
        return {
            "dev": dev, "ino": ino, "ofs": ofs, "pageIdx": ofs // args.page_size,
            "filename": filenames.get((dev, ino), ""),
            "maxMmapcnt": st[P_MMAPCNT], "owningProcess": pid,
            "inAdd": st[P_IN_ADD], "inBitmap": st[P_IN_BITMAP],
        }

    if qualifying_keys:
        print("", file=sys.stderr)
        print(f"Top {min(args.top, len(qualifying_keys))} single-owner unmapped pages:", file=sys.stderr)
        header = f"{'dev':>8} {'ino':>12} {'ofs':>12} {'pageIdx':>9}  owningProcess  filename"
        print(header, file=sys.stderr)
        for key in qualifying_keys[: args.top]:
            r = qual_row(key)
            print(f"{r['dev']:>8} {r['ino']:>12} {r['ofs']:>12} {r['pageIdx']:>9}  "
                  f"{r['owningProcess']}  {r['filename']}", file=sys.stderr)

    if args.json:
        stats["params"] = {
            "start": args.start, "end": args.end, "pageSize": args.page_size,
            "useAccess": args.use_access, "useBitmapPid": args.use_bitmap_pid,
        }
        stats["anomalies"] = dict(anomalies)
        stats["qualifyingPages"] = [qual_row(k) for k in qualifying_keys]
        with open(args.json, "w") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
        print(f"Wrote {os.path.abspath(args.json)}", file=sys.stderr)

    if args.csv:
        with open(args.csv, "w") as f:
            f.write("dev,ino,ofs,page_idx,filename,max_mmapcnt,owning_process,in_add,in_bitmap\n")
            for key in qualifying_keys:
                r = qual_row(key)
                fn = str(r["filename"]).replace('"', '""')
                f.write(
                    f"{r['dev']},{r['ino']},{r['ofs']},{r['pageIdx']},\"{fn}\","
                    f"{r['maxMmapcnt']},{r['owningProcess']},{r['inAdd']},{r['inBitmap']}\n"
                )
        print(f"Wrote {os.path.abspath(args.csv)}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
