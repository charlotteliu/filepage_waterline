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
  - The bitmap snapshot's `pid_name` is the PID of the `cat`-style shell
    command that dumped the bitmap, not a real accessor — it is never
    counted as a touching process.
  - "Touched by a process" is a priority cascade, not a union: if the page
    has any `mm_filemap_access_history` rows, its owner is decided by
    *those rows only*; else if it has any add rows, by add's; else
    (add- and access-less, delete only) by delete's. `--no-access` removes
    access-history from consideration (falls through to add, then delete).

Memory model (matters for 10s-of-GB DBs with 10s-100s of millions of
distinct pages): the naive version of this (a dict keyed by the full
`(dev,ino,ofs)` tuple, with a Python `set()` per page per event source)
measured at 600-900 bytes/page in practice — mostly the fixed ~216-byte
CPython overhead of *each empty set object*, tripled, plus a fresh 3-tuple
key per page even though `dev`/`ino` repeat across every page of a file.
That is what drove a 30GB DB past 20GB RSS. This version instead:
  - Nests the aggregate as `{file_key -> {ofs -> state}}` (`file_key` =
    interned `"dev|ino"`, same convention as `cold_page_stats.py`), so
    `dev`/`ino` are stored once per *file*, not once per *page*.
  - Packs the 6 boolean/flag bits (in_add, in_bitmap, has_mmapcnt, and one
    "saw >1 distinct pid" bit per event source) into a single int, and
    tracks each source's owning pid as a single nullable string slot
    instead of a `set()` — sufficient because we only ever need to know
    "is it exactly one distinct pid", never the full membership.
  - Interns `pid_name` strings, which recur across millions of rows drawn
    from a small process-name vocabulary.
  - Frees the whole aggregate (`del agg`) as soon as the single summary
    pass over it has extracted what's needed for reporting, instead of
    holding it for the rest of the program.
  - Only materializes as many qualifying-page rows as will actually be
    printed/exported (`--top` bound) unless `--json`/`--csv` asks for the
    full list — the running *count* is always exact regardless.
This cuts the measured per-page footprint by roughly 5-8x (see the devlog
for the tracemalloc benchmark) without changing any result.

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

# Per-page state: [flags, max_mmapcnt, add_pid, delete_pid, access_pid].
# add_pid/delete_pid/access_pid hold the *first* pid_name seen from that
# source, or None if that source never touched this page; the matching
# "_MULTI" flag bit is set the first time a *different* pid_name shows up
# from that same source. This is enough to answer "exactly one distinct
# pid from this source?" without ever storing a full set.
S_FLAGS, S_MMAPCNT, S_ADD_PID, S_DEL_PID, S_ACCESS_PID = range(5)

F_IN_ADD = 1
F_IN_BITMAP = 2
F_HAS_MMAPCNT = 4
F_ADD_MULTI = 8
F_DEL_MULTI = 16
F_ACCESS_MULTI = 32

_OWNER_SOURCES = (
    (S_ACCESS_PID, F_ACCESS_MULTI, "access"),
    (S_ADD_PID, F_ADD_MULTI, "add"),
    (S_DEL_PID, F_DEL_MULTI, "delete"),
)


def make_file_key(dev: str, ino: str) -> str:
    return f"{dev}|{ino}"


def split_file_key(key: str) -> Tuple[str, str]:
    dev, _, ino = key.partition("|")
    return dev, ino


def touch_pid(st: list, pid_slot: int, multi_bit: int, pid_name: str) -> None:
    if not pid_name:
        return
    cur = st[pid_slot]
    if cur is None:
        st[pid_slot] = pid_name
    elif cur != pid_name:
        st[S_FLAGS] |= multi_bit


def owner_info(st: list) -> Tuple[Optional[str], bool, str]:
    """access > add > delete priority cascade. Returns (pid or None,
    is_single_owner, source_name)."""
    for pid_slot, multi_bit, source in _OWNER_SOURCES:
        pid = st[pid_slot]
        if pid is not None:
            return pid, not (st[S_FLAGS] & multi_bit), source
    return None, False, "none"


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
    use_access: bool,
) -> Tuple[Dict[str, Dict[int, list]], collections.Counter]:
    agg: Dict[str, Dict[int, list]] = {}
    anomalies: collections.Counter = collections.Counter()
    intern = sys.intern

    def get(file_key: str, ofs: int) -> list:
        file_map = agg.get(file_key)
        if file_map is None:
            file_map = {}
            agg[file_key] = file_map
        st = file_map.get(ofs)
        if st is None:
            st = [0, 0, None, None, None]
            file_map[ofs] = st
        return st

    def peek(file_key: str, ofs: int) -> Optional[list]:
        file_map = agg.get(file_key)
        return file_map.get(ofs) if file_map is not None else None

    where, params = _time_where(start, end)

    # bitmap: population membership only. Its pid_name is the shell command
    # (e.g. `cat`) that dumped the bitmap, not a real accessor -> not read.
    cur = conn.execute(f"SELECT dev, ino, page_ofs FROM {BITMAP_TABLE} {where}", params)
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        for r in rows:
            file_key = intern(make_file_key(r["dev"], r["ino"]))
            st = get(file_key, int(r["page_ofs"]))
            st[S_FLAGS] |= F_IN_BITMAP

    # add: population membership + mmapcnt + pid (lowest-priority owner source).
    cur = conn.execute(f"SELECT dev, ino, ofs, mmapcnt, pid_name FROM {ADD_TABLE} {where}", params)
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        for r in rows:
            file_key = intern(make_file_key(r["dev"], r["ino"]))
            st = get(file_key, int(r["ofs"]))
            st[S_FLAGS] |= F_IN_ADD | F_HAS_MMAPCNT
            mmapcnt = int(r["mmapcnt"] or 0)
            if mmapcnt > st[S_MMAPCNT]:
                st[S_MMAPCNT] = mmapcnt
            pid_name = r["pid_name"]
            if pid_name:
                touch_pid(st, S_ADD_PID, F_ADD_MULTI, intern(str(pid_name)))

    # delete: enrich mmapcnt + pid (last-resort owner source); does not, by
    # itself, define population.
    if table_exists(conn, DELETE_TABLE):
        cur = conn.execute(f"SELECT dev, ino, ofs, mmapcnt, pid_name FROM {DELETE_TABLE} {where}", params)
        while True:
            rows = cur.fetchmany(50000)
            if not rows:
                break
            for r in rows:
                file_key = intern(make_file_key(r["dev"], r["ino"]))
                ofs = int(r["ofs"])
                if peek(file_key, ofs) is None:
                    anomalies["delete_without_add_or_bitmap"] += 1
                st = get(file_key, ofs)  # keep the data; excluded from population at aggregation time
                st[S_FLAGS] |= F_HAS_MMAPCNT
                mmapcnt = int(r["mmapcnt"] or 0)
                if mmapcnt > st[S_MMAPCNT]:
                    st[S_MMAPCNT] = mmapcnt
                pid_name = r["pid_name"]
                if pid_name:
                    touch_pid(st, S_DEL_PID, F_DEL_MULTI, intern(str(pid_name)))

    # access_history: enrich mmapcnt + pid; when present, its pid is the
    # highest-priority owner source (see owner_info()).
    if use_access and table_exists(conn, ACCESS_TABLE):
        cur = conn.execute(f"SELECT dev, ino, ofs, mmapcnt, pid_name FROM {ACCESS_TABLE} {where}", params)
        while True:
            rows = cur.fetchmany(50000)
            if not rows:
                break
            for r in rows:
                file_key = intern(make_file_key(r["dev"], r["ino"]))
                ofs = int(r["ofs"])
                if peek(file_key, ofs) is None:
                    anomalies["access_without_add_or_bitmap"] += 1
                st = get(file_key, ofs)
                st[S_FLAGS] |= F_HAS_MMAPCNT
                mmapcnt = int(r["mmapcnt"] or 0)
                if mmapcnt > st[S_MMAPCNT]:
                    st[S_MMAPCNT] = mmapcnt
                pid_name = r["pid_name"]
                if pid_name:
                    touch_pid(st, S_ACCESS_PID, F_ACCESS_MULTI, intern(str(pid_name)))

    return agg, anomalies


def analyze(agg: Dict[str, Dict[int, list]], keep_limit: Optional[int]) -> dict:
    """Single pass over the aggregate. `keep_limit` bounds how many
    qualifying-page rows are materialized (None = keep all, needed for
    --json/--csv); the reported counts are always exact regardless."""
    pop_count = 0
    in_add_only = in_bitmap_only = in_both = 0
    no_mmapcnt_data = 0
    qual_count = 0
    qualifying_rows: List[dict] = []

    for file_key, file_map in agg.items():
        dev, ino = split_file_key(file_key)
        for ofs, st in file_map.items():
            flags = st[S_FLAGS]
            in_add = bool(flags & F_IN_ADD)
            in_bitmap = bool(flags & F_IN_BITMAP)
            if not (in_add or in_bitmap):
                continue  # delete/access-only anomaly entry, not population

            pop_count += 1
            if in_add and in_bitmap:
                in_both += 1
            elif in_add:
                in_add_only += 1
            else:
                in_bitmap_only += 1

            if not (flags & F_HAS_MMAPCNT):
                no_mmapcnt_data += 1
                continue  # bitmap-only page: no mmapcnt data -> can't be "mmapcnt==0"
            if st[S_MMAPCNT] != 0:
                continue

            pid, is_single, source = owner_info(st)
            if pid is None or not is_single:
                continue

            qual_count += 1
            if keep_limit is None or len(qualifying_rows) < keep_limit:
                qualifying_rows.append({
                    "dev": dev, "ino": ino, "ofs": ofs,
                    "maxMmapcnt": st[S_MMAPCNT],
                    "owningProcess": pid, "ownerSource": source,
                    "inAdd": in_add, "inBitmap": in_bitmap,
                })

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
        "_qualifying_rows": qualifying_rows,
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
                   help="Don't use mm_filemap_access_history at all (owner falls through to add, then delete).")
    p.set_defaults(use_access=True)
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
    agg, anomalies = collect(conn, args.start, args.end, args.use_access)

    need_full_export = bool(args.json or args.csv)
    stats = analyze(agg, keep_limit=None if need_full_export else args.top)
    qualifying_rows = stats.pop("_qualifying_rows")
    del agg  # the aggregate can be tens of GB on large DBs; drop it before any reporting work

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
    if table_exists(conn, INODE_TABLE) and qualifying_rows:
        filenames = resolve_filenames_by_ino(conn, {(r["dev"], r["ino"]) for r in qualifying_rows})
    for r in qualifying_rows:
        r["pageIdx"] = r["ofs"] // args.page_size
        r["filename"] = filenames.get((r["dev"], r["ino"]), "")

    if qualifying_rows:
        print("", file=sys.stderr)
        print(f"Top {min(args.top, len(qualifying_rows))} single-owner unmapped pages:", file=sys.stderr)
        header = f"{'dev':>8} {'ino':>12} {'ofs':>12} {'pageIdx':>9}  owningProcess(source)  filename"
        print(header, file=sys.stderr)
        for r in qualifying_rows[: args.top]:
            print(f"{r['dev']:>8} {r['ino']:>12} {r['ofs']:>12} {r['pageIdx']:>9}  "
                  f"{r['owningProcess']}({r['ownerSource']})  {r['filename']}", file=sys.stderr)

    if args.json:
        stats["params"] = {
            "start": args.start, "end": args.end, "pageSize": args.page_size,
            "useAccess": args.use_access,
        }
        stats["anomalies"] = dict(anomalies)
        stats["qualifyingPages"] = qualifying_rows
        with open(args.json, "w") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
        print(f"Wrote {os.path.abspath(args.json)}", file=sys.stderr)

    if args.csv:
        with open(args.csv, "w") as f:
            f.write("dev,ino,ofs,page_idx,filename,max_mmapcnt,owning_process,owner_source,in_add,in_bitmap\n")
            for r in qualifying_rows:
                fn = str(r["filename"]).replace('"', '""')
                f.write(
                    f"{r['dev']},{r['ino']},{r['ofs']},{r['pageIdx']},\"{fn}\","
                    f"{r['maxMmapcnt']},{r['owningProcess']},{r['ownerSource']},{r['inAdd']},{r['inBitmap']}\n"
                )
        print(f"Wrote {os.path.abspath(args.csv)}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
