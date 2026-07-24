#!/usr/bin/env python3
"""Cold-page statistics over the ftrace SQLite DB (non-visual).

A file page is uniquely identified by (dev, ino, ofs). For every add->delete
residency interval observed during the test we count the access events that
land inside it. Access events are:
  - every row of mm_filemap_access_history (mark_access / mark_reaccess /
    mark_referenced), and
  - every mm_filemap_label_page_cache row whose `label` > 1 ("access > 1").

A page interval is COLD when it saw 0 or 1 access between its add and delete.

Cold-page "spacetime" (space x time) is accumulated in page-seconds:
  - 0 accesses -> duration = delete_ts - add_ts
  - 1 access   -> duration = delete_ts - access_ts   (time it sat cold after its
                                                       single touch)
Average cold-page space = total cold spacetime / test duration, i.e. the mean
number of cold pages (and bytes) resident across the whole test window.

Average *resident* space (all closed intervals, not just cold ones) is
computed the same way from each interval's full (delete_ts - add_ts)
duration: total resident spacetime / test duration. Cold space's fraction of
that average resident space (averageColdSpace.fractionOfAverageResident)
shows how much of the "typical" resident footprint at any instant is cold.

Only closed add->delete intervals are counted (a delete is required). Pages
still resident at the end of the window are reported separately, not counted.
"""

import argparse
import collections
import heapq
import itertools
import json
import os
import sqlite3
import sys
from typing import Iterable, List, Optional, Sequence, Tuple

ADD_TABLE = "mm_filemap_add_to_page_cache"
DELETE_TABLE = "mm_filemap_delete_from_page_cache"
ACCESS_TABLE = "mm_filemap_access_history"
LABEL_TABLE = "mm_filemap_label_page_cache"
INODE_TABLE = "inode_mapping"

PAGE_SIZE = 4096
# Event tuple: (timestamp, sort_order, kind, dev, ino, ofs)
Event = Tuple[float, int, str, str, str, int]


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


def has_timestamp_index(conn: sqlite3.Connection, table: str) -> bool:
    if not table_exists(conn, table):
        return False
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        cols = conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
        if cols and cols[0]["name"] == "timestamp":
            return True
    return False


def make_file_key(dev: str, ino: str) -> str:
    return f"{dev}|{ino}"


def split_file_key(key: str) -> Tuple[str, str]:
    dev, _, ino = key.partition("|")
    return dev, ino


def require_tables(conn: sqlite3.Connection) -> None:
    missing = [t for t in (ADD_TABLE, DELETE_TABLE) if not table_exists(conn, t)]
    if missing:
        raise SystemExit(f"Missing required table(s): {', '.join(missing)}")


def used_tables(conn: sqlite3.Connection, use_label: bool) -> List[str]:
    tables = [ADD_TABLE, DELETE_TABLE]
    if table_exists(conn, ACCESS_TABLE):
        tables.append(ACCESS_TABLE)
    if use_label and table_exists(conn, LABEL_TABLE):
        tables.append(LABEL_TABLE)
    return tables


def build_timestamp_indexes(db_path: str, use_label: bool) -> None:
    wconn = connect(db_path, writable=True)
    try:
        for table in used_tables(wconn, use_label):
            if has_timestamp_index(wconn, table):
                continue
            print(f"Building timestamp index on {table} (one-time)...", file=sys.stderr)
            wconn.execute(
                f"CREATE INDEX IF NOT EXISTS ix_{table}_ts ON {table}(timestamp)"
            )
        wconn.commit()
    finally:
        wconn.close()


def _stream(
    conn: sqlite3.Connection,
    table: str,
    kind: str,
    sort_order: int,
    start: Optional[float],
    end: Optional[float],
    label_min: Optional[int],
) -> Iterable[Event]:
    clauses: List[str] = []
    params: List[object] = []
    if start is not None:
        clauses.append("t.timestamp >= ?")
        params.append(start)
    if end is not None:
        clauses.append("t.timestamp <= ?")
        params.append(end)
    if label_min is not None:
        clauses.append("t.label > ?")
        params.append(label_min)
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT t.timestamp AS timestamp, t.dev AS dev, t.ino AS ino, t.ofs AS ofs "
        f"FROM {table} t {where_sql} ORDER BY t.timestamp"
    )
    cur = conn.execute(sql, params)
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        for row in rows:
            try:
                ofs = int(row["ofs"])
            except (TypeError, ValueError):
                continue
            yield (float(row["timestamp"]), sort_order, kind,
                   str(row["dev"]), str(row["ino"]), ofs)


def iter_events(
    conn: sqlite3.Connection,
    start: Optional[float],
    end: Optional[float],
    use_label: bool,
    label_gt: int,
) -> Iterable[Event]:
    """Merge add/delete/access(+label>N) events in timestamp order.

    Uses per-table timestamp-index cursors + heapq.merge when every source
    table has a timestamp index (no sort); otherwise falls back to a single
    UNION ALL ... ORDER BY (a temp b-tree sort)."""
    have_access = table_exists(conn, ACCESS_TABLE)
    have_label = use_label and table_exists(conn, LABEL_TABLE)
    tables = used_tables(conn, use_label)
    fast = all(has_timestamp_index(conn, t) for t in tables)

    if fast:
        specs = [(ADD_TABLE, "add", 0, None), (DELETE_TABLE, "delete", 2, None)]
        if have_access:
            specs.append((ACCESS_TABLE, "access", 1, None))
        if have_label:
            specs.append((LABEL_TABLE, "access", 1, label_gt))
        streams = [
            _stream(conn, table, kind, order, start, end, lmin)
            for table, kind, order, lmin in specs
        ]
        yield from heapq.merge(*streams, key=lambda e: (e[0], e[1]))
        return

    # Fallback: UNION ALL with a global sort.
    selects: List[str] = []
    params: List[object] = []

    def add_select(table: str, kind: str, order: int, label_min: Optional[int]) -> None:
        clauses = []
        if start is not None:
            clauses.append("t.timestamp >= ?"); params.append(start)
        if end is not None:
            clauses.append("t.timestamp <= ?"); params.append(end)
        if label_min is not None:
            clauses.append("t.label > ?"); params.append(label_min)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        selects.append(
            f"SELECT t.timestamp AS timestamp, {order} AS sort_order, '{kind}' AS kind, "
            f"t.dev AS dev, t.ino AS ino, t.ofs AS ofs FROM {table} t {where}"
        )

    add_select(ADD_TABLE, "add", 0, None)
    add_select(DELETE_TABLE, "delete", 2, None)
    if have_access:
        add_select(ACCESS_TABLE, "access", 1, None)
    if have_label:
        add_select(LABEL_TABLE, "access", 1, label_gt)
    sql = ("SELECT timestamp, sort_order, kind, dev, ino, ofs FROM ("
           + "\nUNION ALL\n".join(selects) + ") ORDER BY timestamp, sort_order")
    cur = conn.execute(sql, params)
    while True:
        rows = cur.fetchmany(50000)
        if not rows:
            break
        for row in rows:
            try:
                ofs = int(row["ofs"])
            except (TypeError, ValueError):
                continue
            yield (float(row["timestamp"]), int(row["sort_order"]), str(row["kind"]),
                   str(row["dev"]), str(row["ino"]), ofs)


def analyze(
    conn: sqlite3.Connection,
    start: Optional[float],
    end: Optional[float],
    use_label: bool,
    label_gt: int,
) -> dict:
    # Per resident page: [add_ts, access_count, first_access_ts]
    A_ADD, A_CNT, A_FIRST = 0, 1, 2
    active: dict = {}
    anomalies = collections.Counter()

    closed = 0
    cold0 = 0
    cold1 = 0
    cold_spacetime = 0.0  # page-seconds
    total_spacetime = 0.0  # page-seconds, every closed interval (not just cold ones)
    # per file: [cold_page_count, cold_spacetime_page_seconds]
    per_file: dict = collections.defaultdict(lambda: [0, 0.0])
    obs_min: Optional[float] = None
    obs_max: Optional[float] = None
    total_events = 0

    for ts, _sort, kind, dev, ino, ofs in iter_events(conn, start, end, use_label, label_gt):
        total_events += 1
        if obs_min is None:
            obs_min = ts
        obs_max = ts
        pk = (dev, ino, ofs)

        if kind == "add":
            if pk in active:
                anomalies["duplicate_add_dropped_previous"] += 1
            active[pk] = [ts, 0, None]
        elif kind == "access":
            st = active.get(pk)
            if st is None:
                anomalies["access_without_active_add"] += 1
            else:
                st[A_CNT] += 1
                if st[A_FIRST] is None:
                    st[A_FIRST] = ts
        elif kind == "delete":
            st = active.pop(pk, None)
            if st is None:
                anomalies["delete_without_active_add"] += 1
                continue
            closed += 1
            full_dur = ts - st[A_ADD]
            total_spacetime += full_dur if full_dur > 0 else 0.0
            acc = st[A_CNT]
            if acc <= 1:
                if acc == 0:
                    dur = ts - st[A_ADD]
                    cold0 += 1
                else:
                    dur = ts - (st[A_FIRST] if st[A_FIRST] is not None else st[A_ADD])
                    cold1 += 1
                if dur < 0:
                    dur = 0.0
                cold_spacetime += dur
                fk = make_file_key(dev, ino)
                pf = per_file[fk]
                pf[0] += 1
                pf[1] += dur

    open_at_end = len(active)
    if start is not None:
        obs_min = start if obs_min is None else min(obs_min, start)
    if end is not None:
        obs_max = end if obs_max is None else max(obs_max, end)
    test_time = max((obs_max or 0.0) - (obs_min or 0.0), 1e-9)

    cold_pages = cold0 + cold1
    # Average cold-page footprint over the whole test window.
    avg_cold_pages = cold_spacetime / test_time
    avg_cold_bytes = avg_cold_pages * PAGE_SIZE
    # Average *resident* footprint (all closed intervals) over the same window.
    avg_resident_pages = total_spacetime / test_time
    avg_resident_bytes = avg_resident_pages * PAGE_SIZE
    cold_fraction_of_resident = (avg_cold_pages / avg_resident_pages) if avg_resident_pages > 0 else 0.0

    return {
        "params": {
            "start": obs_min,
            "end": obs_max,
            "testTimeSeconds": round(test_time, 6),
            "pageSize": PAGE_SIZE,
            "useLabelAccess": use_label,
            "labelAccessGt": label_gt,
            "totalEvents": total_events,
        },
        "intervals": {
            "closedAddDelete": closed,
            "coldPages": cold_pages,
            "coldZeroAccess": cold0,
            "coldOneAccess": cold1,
            "coldFraction": round(cold_pages / closed, 6) if closed else 0.0,
            "stillResidentAtEnd": open_at_end,
        },
        "spacetime": {
            "coldPageSeconds": round(cold_spacetime, 6),
            "coldByteSeconds": round(cold_spacetime * PAGE_SIZE, 3),
            "coldPageMBSeconds": round(cold_spacetime * PAGE_SIZE / (1024 * 1024), 6),
            "residentPageSeconds": round(total_spacetime, 6),
            "residentByteSeconds": round(total_spacetime * PAGE_SIZE, 3),
            "residentPageMBSeconds": round(total_spacetime * PAGE_SIZE / (1024 * 1024), 6),
        },
        "averageColdSpace": {
            "pages": round(avg_cold_pages, 4),
            "bytes": round(avg_cold_bytes, 2),
            "MB": round(avg_cold_bytes / (1024 * 1024), 4),
            "fractionOfAverageResident": round(cold_fraction_of_resident, 6),
        },
        "averageResidentSpace": {
            "pages": round(avg_resident_pages, 4),
            "bytes": round(avg_resident_bytes, 2),
            "MB": round(avg_resident_bytes / (1024 * 1024), 4),
        },
        "anomalies": dict(anomalies),
        "_per_file": per_file,
    }


def top_cold_files(conn, per_file: dict, top: int) -> List[dict]:
    have_inode = table_exists(conn, INODE_TABLE)
    ranked = sorted(per_file.items(), key=lambda kv: kv[1][1], reverse=True)[:top]
    out = []
    for fk, (count, secs) in ranked:
        dev, ino = split_file_key(fk)
        filename = fk
        if have_inode:
            row = conn.execute(
                f"SELECT filename FROM {INODE_TABLE} WHERE dev=? AND ino=?", (dev, ino)
            ).fetchone()
            if row and row["filename"]:
                filename = str(row["filename"])
        out.append({
            "dev": dev, "ino": ino, "filename": filename,
            "coldPages": count,
            "coldPageSeconds": round(secs, 6),
            "coldMBSeconds": round(secs * PAGE_SIZE / (1024 * 1024), 6),
        })
    return out


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Cold-page statistics over the ftrace DB.")
    p.add_argument("--db", required=True, help="Path to the ftrace SQLite DB.")
    p.add_argument("--start", type=float, help="Start timestamp filter.")
    p.add_argument("--end", type=float, help="End timestamp filter.")
    p.add_argument("--top", type=int, default=25, help="Top cold files by spacetime to list.")
    p.add_argument("--json", help="Write full stats JSON to this path.")
    p.add_argument("--csv", help="Write per-file cold stats CSV to this path.")
    p.add_argument("--no-label-access", dest="use_label", action="store_false",
                   help="Ignore label events; count only access_history as accesses.")
    p.set_defaults(use_label=True)
    p.add_argument("--label-access-gt", type=int, default=1,
                   help="A label row counts as an access when label > this (default 1).")
    p.add_argument("--build-indices", action="store_true",
                   help="Create missing timestamp indexes (writes to DB, one-time).")
    return p.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    if args.build_indices:
        build_timestamp_indexes(args.db, args.use_label)
    conn = connect(args.db)
    require_tables(conn)
    tables = used_tables(conn, args.use_label)
    if not all(has_timestamp_index(conn, t) for t in tables):
        print("WARNING: some event tables lack a timestamp index; using a full sort "
              "(slow on large DBs). Re-run with --build-indices once.", file=sys.stderr)

    print("Analyzing cold pages...", file=sys.stderr)
    stats = analyze(conn, args.start, args.end, args.use_label, args.label_access_gt)
    per_file = stats.pop("_per_file")
    stats["topColdFiles"] = top_cold_files(conn, per_file, args.top)

    iv = stats["intervals"]
    sp = stats["spacetime"]
    av = stats["averageColdSpace"]
    ar = stats["averageResidentSpace"]
    pr = stats["params"]
    print("", file=sys.stderr)
    print(f"Test window        : {pr['start']:.6f}..{pr['end']:.6f}  "
          f"({pr['testTimeSeconds']:.3f}s), events={pr['totalEvents']:,}", file=sys.stderr)
    print(f"Closed add->delete : {iv['closedAddDelete']:,}", file=sys.stderr)
    print(f"Cold pages (<=1 acc): {iv['coldPages']:,}  "
          f"(0-access {iv['coldZeroAccess']:,}, 1-access {iv['coldOneAccess']:,}, "
          f"{iv['coldFraction']*100:.1f}% of closed)", file=sys.stderr)
    print(f"Still resident @end : {iv['stillResidentAtEnd']:,} (excluded)", file=sys.stderr)
    print(f"Cold spacetime     : {sp['coldPageSeconds']:,.3f} page-s  "
          f"({sp['coldPageMBSeconds']:,.3f} MB-s)", file=sys.stderr)
    print(f"Avg resident space : {ar['pages']:,.2f} pages  "
          f"({ar['MB']:,.3f} MB) resident on average (all closed intervals)", file=sys.stderr)
    print(f"Avg cold space     : {av['pages']:,.2f} pages  "
          f"({av['MB']:,.3f} MB) resident-cold on average  "
          f"({av['fractionOfAverageResident']*100:.1f}% of avg resident space)", file=sys.stderr)
    if stats["anomalies"]:
        print(f"Anomalies          : {stats['anomalies']}", file=sys.stderr)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
        print(f"Wrote {os.path.abspath(args.json)}", file=sys.stderr)
    if args.csv:
        with open(args.csv, "w") as f:
            f.write("dev,ino,filename,cold_pages,cold_page_seconds,cold_mb_seconds\n")
            for r in top_cold_files(conn, per_file, len(per_file)):
                fn = str(r["filename"]).replace('"', '""')
                f.write(f'{r["dev"]},{r["ino"]},"{fn}",{r["coldPages"]},'
                        f'{r["coldPageSeconds"]},{r["coldMBSeconds"]}\n')
        print(f"Wrote {os.path.abspath(args.csv)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
