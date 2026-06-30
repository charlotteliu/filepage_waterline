#!/usr/bin/env python3
"""
Reconstruct Linux file-page-cache residency lifecycles from the ftrace SQLite DB.

The DB schema is expected to match the tables documented in fscachedb.md:
  - inode_mapping
  - mm_filemap_add_to_page_cache
  - mm_filemap_delete_from_page_cache
  - mm_filemap_access_history
  - timestep

The script streams add/access/delete events ordered by timestamp, reconstructs
resident pages keyed by (dev, ino, ofs), and writes a standalone HTML viewer.
"""

import argparse
import collections
import json
import math
import os
import sqlite3
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


ADD_TABLE = "mm_filemap_add_to_page_cache"
DELETE_TABLE = "mm_filemap_delete_from_page_cache"
ACCESS_TABLE = "mm_filemap_access_history"
INODE_TABLE = "inode_mapping"
TIMESTEP_TABLE = "timestep"
RESULT_TABLE = "result_adddel_only_mmapcnt0_access0"

PAGE_SIZE = 4096
Event = Tuple[float, int, str, str, str, int, str]
PageKey = Tuple[str, str, int]
FileKey = Tuple[str, str]


def connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise SystemExit(f"DB not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def require_tables(conn: sqlite3.Connection, include_access: bool) -> None:
    required = [ADD_TABLE, DELETE_TABLE]
    if include_access:
        required.append(ACCESS_TABLE)
    missing = [name for name in required if not table_exists(conn, name)]
    if missing:
        raise SystemExit(f"Missing required table(s): {', '.join(missing)}")


def load_inode_mapping(conn: sqlite3.Connection) -> Dict[FileKey, Dict[str, object]]:
    if not table_exists(conn, INODE_TABLE):
        return {}
    mapping: Dict[FileKey, Dict[str, object]] = {}
    for row in conn.execute(f"SELECT dev, ino, size, filename FROM {INODE_TABLE}"):
        mapping[(str(row["dev"]), str(row["ino"]))] = {
            "dev": str(row["dev"]),
            "ino": str(row["ino"]),
            "size": int(row["size"] or 0),
            "filename": str(row["filename"] or ""),
        }
    return mapping


def build_table_filter(
    alias: str,
    start: Optional[float],
    end: Optional[float],
    pid_like: Optional[str],
    file_like: Optional[str],
) -> Tuple[str, List[object]]:
    clauses: List[str] = []
    params: List[object] = []
    if start is not None:
        clauses.append(f"{alias}.timestamp >= ?")
        params.append(start)
    if end is not None:
        clauses.append(f"{alias}.timestamp <= ?")
        params.append(end)
    if pid_like:
        clauses.append(f"{alias}.pid_name LIKE ?")
        params.append(pid_like)
    if file_like:
        clauses.append(
            "EXISTS ("
            f"SELECT 1 FROM {INODE_TABLE} im "
            f"WHERE im.dev = {alias}.dev AND im.ino = {alias}.ino "
            "AND im.filename LIKE ?)"
        )
        params.append(file_like)
    if clauses:
        return "WHERE " + " AND ".join(clauses), params
    return "", params


def event_query(
    conn: sqlite3.Connection,
    include_access: bool,
    start: Optional[float],
    end: Optional[float],
    pid_like: Optional[str],
    file_like: Optional[str],
    event_limit: Optional[int],
) -> Tuple[str, List[object]]:
    selects: List[str] = []
    params: List[object] = []

    for kind, sort_order, table in [
        ("add", 0, ADD_TABLE),
        ("delete", 2, DELETE_TABLE),
    ]:
        where_sql, where_params = build_table_filter(
            "t", start, end, pid_like, file_like
        )
        selects.append(
            "SELECT t.timestamp AS timestamp, "
            f"{sort_order} AS sort_order, "
            f"'{kind}' AS kind, "
            "t.dev AS dev, t.ino AS ino, t.ofs AS ofs, "
            "COALESCE(t.pid_name, '') AS pid_name "
            f"FROM {table} t {where_sql}"
        )
        params.extend(where_params)

    if include_access:
        where_sql, where_params = build_table_filter(
            "t", start, end, pid_like, file_like
        )
        selects.append(
            "SELECT t.timestamp AS timestamp, "
            "1 AS sort_order, "
            "'access' AS kind, "
            "t.dev AS dev, t.ino AS ino, t.ofs AS ofs, "
            "COALESCE(t.pid_name, '') AS pid_name "
            f"FROM {ACCESS_TABLE} t {where_sql}"
        )
        params.extend(where_params)

    sql = (
        "SELECT timestamp, sort_order, kind, dev, ino, ofs, pid_name "
        "FROM (" + "\nUNION ALL\n".join(selects) + ") "
        "ORDER BY timestamp, sort_order"
    )
    if event_limit is not None:
        sql += " LIMIT ?"
        params.append(event_limit)
    return sql, params


def iter_events(
    conn: sqlite3.Connection,
    include_access: bool,
    start: Optional[float],
    end: Optional[float],
    pid_like: Optional[str],
    file_like: Optional[str],
    event_limit: Optional[int],
) -> Iterable[Event]:
    sql, params = event_query(
        conn, include_access, start, end, pid_like, file_like, event_limit
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
            yield (
                float(row["timestamp"]),
                int(row["sort_order"]),
                str(row["kind"]),
                str(row["dev"]),
                str(row["ino"]),
                ofs,
                str(row["pid_name"] or "unknown"),
            )


def min_max_timestamp(
    conn: sqlite3.Connection,
    include_access: bool,
    start: Optional[float],
    end: Optional[float],
    pid_like: Optional[str],
    file_like: Optional[str],
    event_limit: Optional[int],
) -> Tuple[float, float]:
    if event_limit is not None:
        first = last = None
        for event in iter_events(
            conn, include_access, start, end, pid_like, file_like, event_limit
        ):
            ts = event[0]
            if first is None:
                first = ts
            last = ts
        if first is None or last is None:
            raise SystemExit("No matching add/delete/access events found.")
        return first, last

    selects: List[str] = []
    params: List[object] = []
    for table in [ADD_TABLE, DELETE_TABLE] + ([ACCESS_TABLE] if include_access else []):
        where_sql, where_params = build_table_filter(
            "t", start, end, pid_like, file_like
        )
        selects.append(f"SELECT t.timestamp AS timestamp FROM {table} t {where_sql}")
        params.extend(where_params)
    row = conn.execute(
        "SELECT MIN(timestamp) AS min_ts, MAX(timestamp) AS max_ts FROM ("
        + "\nUNION ALL\n".join(selects)
        + ")",
        params,
    ).fetchone()
    if row is None or row["min_ts"] is None or row["max_ts"] is None:
        raise SystemExit("No matching add/delete/access events found.")
    return float(row["min_ts"]), float(row["max_ts"])


def file_label(file_key: FileKey, mapping: Dict[FileKey, Dict[str, object]]) -> str:
    meta = mapping.get(file_key)
    if meta and meta.get("filename"):
        return str(meta["filename"])
    return f"{file_key[0]}:{file_key[1]}"


def safe_pid(pid_name: str) -> str:
    return pid_name or "unknown"


def make_file_key(dev: str, ino: str) -> str:
    return f"{dev}|{ino}"


def split_file_key(key: str) -> FileKey:
    if "|" not in key:
        return key, ""
    dev, ino = key.split("|", 1)
    return dev, ino


def make_file_pid_key(file_key: str, pid_name: str) -> str:
    return f"{file_key}\u001f{pid_name}"


def split_file_pid_key(key: str) -> Tuple[str, str]:
    if "\u001f" not in key:
        return key, ""
    return tuple(key.split("\u001f", 1))  # type: ignore[return-value]


def load_candidate_pages(
    conn: sqlite3.Connection,
    max_lanes: int,
    start: Optional[float],
    end: Optional[float],
    pid_like: Optional[str],
    file_like: Optional[str],
) -> List[PageKey]:
    if max_lanes <= 0:
        return []

    params: List[object] = []
    if table_exists(conn, RESULT_TABLE):
        clauses: List[str] = []
        if file_like and table_exists(conn, INODE_TABLE):
            clauses.append(
                "EXISTS (SELECT 1 FROM inode_mapping im "
                "WHERE im.dev = r.dev AND im.ino = r.ino AND im.filename LIKE ?)"
            )
            params.append(file_like)
        if start is not None:
            clauses.append("r.last_ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("r.first_ts <= ?")
            params.append(end)
        where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            "SELECT r.dev, r.ino, r.ofs "
            f"FROM {RESULT_TABLE} r {where_sql} "
            "ORDER BY COALESCE(r.add_del_count, 0) DESC, "
            "COALESCE(r.last_ts, 0) - COALESCE(r.first_ts, 0) DESC "
            "LIMIT ?"
        )
        rows = conn.execute(sql, [*params, max_lanes]).fetchall()
        if rows:
            return [(str(r["dev"]), str(r["ino"]), int(r["ofs"])) for r in rows]

    where_sql, where_params = build_table_filter(
        "t", start, end, pid_like, file_like
    )
    sql = (
        f"SELECT t.dev, t.ino, t.ofs FROM {ADD_TABLE} t {where_sql} "
        "ORDER BY t.timestamp LIMIT ?"
    )
    rows = conn.execute(sql, [*where_params, max_lanes]).fetchall()
    return [(str(r["dev"]), str(r["ino"]), int(r["ofs"])) for r in rows]


def load_timesteps(
    conn: sqlite3.Connection, start: float, end: float
) -> List[Dict[str, object]]:
    if not table_exists(conn, TIMESTEP_TABLE):
        return []
    rows = conn.execute(
        f"SELECT timestamp, step FROM {TIMESTEP_TABLE} "
        "WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
        (start, end),
    ).fetchall()
    return [{"timestamp": float(r["timestamp"]), "step": str(r["step"])} for r in rows]


def choose_bucket_seconds(
    start_ts: float, end_ts: float, requested: Optional[float], target_points: int
) -> float:
    if requested and requested > 0:
        return requested
    span = max(end_ts - start_ts, 0.001)
    raw = span / max(target_points, 1)
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for factor in (1, 2, 5, 10):
        value = factor * magnitude
        if value >= raw:
            return max(value, 0.001)
    return max(raw, 0.001)


def top_counter(counter: collections.Counter, limit: int) -> Dict[str, int]:
    return {str(k): int(v) for k, v in counter.most_common(limit) if v}


def pass_for_peaks(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    start_ts: float,
    end_ts: float,
    bucket_seconds: float,
    mapping: Dict[FileKey, Dict[str, object]],
) -> Tuple[List[str], List[str], List[str], Dict[str, object]]:
    active: Dict[PageKey, Tuple[str, str]] = {}
    by_file: collections.Counter = collections.Counter()
    by_pid: collections.Counter = collections.Counter()
    by_file_pid: collections.Counter = collections.Counter()
    peak_file: collections.Counter = collections.Counter()
    peak_pid: collections.Counter = collections.Counter()
    peak_file_pid: collections.Counter = collections.Counter()
    anomalies = collections.Counter()
    total_events = 0
    next_bucket = start_ts

    def snapshot_peaks() -> None:
        for key, value in by_file.items():
            if value > peak_file[key]:
                peak_file[key] = value
        for key, value in by_pid.items():
            if value > peak_pid[key]:
                peak_pid[key] = value
        for key, value in by_file_pid.items():
            if value > peak_file_pid[key]:
                peak_file_pid[key] = value

    for ts, _sort, kind, dev, ino, ofs, pid_name in iter_events(
        conn,
        args.include_access,
        args.start,
        args.end,
        args.pid_like,
        args.file_like,
        args.event_limit,
    ):
        while next_bucket <= ts:
            snapshot_peaks()
            next_bucket += bucket_seconds
        total_events += 1
        page_key = (dev, ino, ofs)
        file_key = make_file_key(dev, ino)
        pid = safe_pid(pid_name)

        if kind == "add":
            if page_key in active:
                old_file, old_pid = active[page_key]
                by_file[old_file] -= 1
                by_pid[old_pid] -= 1
                by_file_pid[make_file_pid_key(old_file, old_pid)] -= 1
                anomalies["duplicate_add_closed_previous"] += 1
            active[page_key] = (file_key, pid)
            by_file[file_key] += 1
            by_pid[pid] += 1
            by_file_pid[make_file_pid_key(file_key, pid)] += 1
        elif kind == "delete":
            old = active.pop(page_key, None)
            if old is None:
                anomalies["delete_without_active_add"] += 1
            else:
                old_file, old_pid = old
                by_file[old_file] -= 1
                by_pid[old_pid] -= 1
                by_file_pid[make_file_pid_key(old_file, old_pid)] -= 1
        elif kind == "access":
            old = active.get(page_key)
            if old is None:
                anomalies["access_without_active_add"] += 1
            else:
                old_file, old_pid = old
                if pid != old_pid:
                    by_pid[old_pid] -= 1
                    by_pid[pid] += 1
                    by_file_pid[make_file_pid_key(old_file, old_pid)] -= 1
                    by_file_pid[make_file_pid_key(old_file, pid)] += 1
                    active[page_key] = (old_file, pid)

    while next_bucket <= end_ts + 1e-9:
        snapshot_peaks()
        next_bucket += bucket_seconds

    top_files = [k for k, _v in peak_file.most_common(args.max_groups)]
    top_pids = [k for k, _v in peak_pid.most_common(args.max_groups)]
    top_file_pids = [k for k, _v in peak_file_pid.most_common(args.max_groups)]
    stats = {
        "totalEvents": total_events,
        "openPagesAtEnd": len(active),
        "anomalies": dict(anomalies),
        "peakResidentPages": int(sum(max(v, 0) for v in by_file.values())),
    }
    return top_files, top_pids, top_file_pids, stats


def reconstruct(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    start_ts: float,
    end_ts: float,
    bucket_seconds: float,
    mapping: Dict[FileKey, Dict[str, object]],
    top_files: Sequence[str],
    top_pids: Sequence[str],
    top_file_pids: Sequence[str],
    candidate_pages: Sequence[PageKey],
) -> Dict[str, object]:
    active: Dict[PageKey, Dict[str, object]] = {}
    by_file: collections.Counter = collections.Counter()
    by_pid: collections.Counter = collections.Counter()
    by_file_pid: collections.Counter = collections.Counter()
    anomalies = collections.Counter()
    series: List[Dict[str, object]] = []
    lanes: Dict[PageKey, Dict[str, object]] = {}
    lane_order = {page: i for i, page in enumerate(candidate_pages)}
    top_file_set = set(top_files)
    top_pid_set = set(top_pids)
    top_file_pid_set = set(top_file_pids)
    next_bucket = start_ts
    total_events = 0

    def emit_snapshot(ts: float) -> None:
        other_file = sum(max(0, v) for k, v in by_file.items() if k not in top_file_set)
        other_pid = sum(max(0, v) for k, v in by_pid.items() if k not in top_pid_set)
        other_file_pid = sum(
            max(0, v) for k, v in by_file_pid.items() if k not in top_file_pid_set
        )
        by_file_payload = {k: int(max(0, by_file[k])) for k in top_files}
        by_pid_payload = {k: int(max(0, by_pid[k])) for k in top_pids}
        by_file_pid_payload = {k: int(max(0, by_file_pid[k])) for k in top_file_pids}
        if other_file:
            by_file_payload["__other__"] = int(other_file)
        if other_pid:
            by_pid_payload["__other__"] = int(other_pid)
        if other_file_pid:
            by_file_pid_payload["__other__"] = int(other_file_pid)
        series.append(
            {
                "t": round(ts, 6),
                "total": int(sum(max(0, v) for v in by_file.values())),
                "byFile": by_file_payload,
                "byPid": by_pid_payload,
                "byFilePid": by_file_pid_payload,
            }
        )

    def ensure_lane(page_key: PageKey) -> Optional[Dict[str, object]]:
        if page_key not in lane_order:
            return None
        lane = lanes.get(page_key)
        if lane is not None:
            return lane
        dev, ino, ofs = page_key
        fk = (dev, ino)
        lane = {
            "id": f"{dev}|{ino}|{ofs}",
            "dev": dev,
            "ino": ino,
            "ofs": ofs,
            "pageIndex": ofs // PAGE_SIZE if ofs >= 0 else None,
            "fileKey": make_file_key(dev, ino),
            "fileLabel": file_label(fk, mapping),
            "segments": [],
            "events": [],
        }
        lanes[page_key] = lane
        return lane

    def add_lane_event(page_key: PageKey, ts: float, kind: str, pid: str) -> None:
        lane = ensure_lane(page_key)
        if lane is None:
            return
        events = lane["events"]
        if len(events) < args.max_lane_events:
            events.append({"t": round(ts, 6), "kind": kind, "pidName": pid})

    def close_lane_segment(page_key: PageKey, ts: float, reason: str) -> None:
        lane = ensure_lane(page_key)
        state = active.get(page_key)
        if lane is None or state is None or state.get("segmentStart") is None:
            return
        start = float(state["segmentStart"])
        if ts < start:
            ts = start
        lane["segments"].append(
            {
                "start": round(start, 6),
                "end": round(ts, 6),
                "fileKey": state["fileKey"],
                "pidName": state["pidName"],
                "reason": reason,
                "accessCount": int(state.get("segmentAccessCount", 0)),
            }
        )
        state["segmentStart"] = ts
        state["segmentAccessCount"] = 0

    for ts, _sort, kind, dev, ino, ofs, pid_name in iter_events(
        conn,
        args.include_access,
        args.start,
        args.end,
        args.pid_like,
        args.file_like,
        args.event_limit,
    ):
        while next_bucket <= ts:
            emit_snapshot(next_bucket)
            next_bucket += bucket_seconds

        total_events += 1
        page_key = (dev, ino, ofs)
        file_key = make_file_key(dev, ino)
        pid = safe_pid(pid_name)

        if kind == "add":
            if page_key in active:
                close_lane_segment(page_key, ts, "duplicate_add")
                old = active[page_key]
                by_file[old["fileKey"]] -= 1
                by_pid[old["pidName"]] -= 1
                by_file_pid[make_file_pid_key(old["fileKey"], old["pidName"])] -= 1
                anomalies["duplicate_add_closed_previous"] += 1
            active[page_key] = {
                "fileKey": file_key,
                "pidName": pid,
                "start": ts,
                "segmentStart": ts,
                "segmentAccessCount": 0,
            }
            by_file[file_key] += 1
            by_pid[pid] += 1
            by_file_pid[make_file_pid_key(file_key, pid)] += 1
            add_lane_event(page_key, ts, "add", pid)
        elif kind == "delete":
            state = active.get(page_key)
            add_lane_event(page_key, ts, "delete", pid)
            if state is None:
                anomalies["delete_without_active_add"] += 1
            else:
                close_lane_segment(page_key, ts, "delete")
                by_file[state["fileKey"]] -= 1
                by_pid[state["pidName"]] -= 1
                by_file_pid[make_file_pid_key(state["fileKey"], state["pidName"])] -= 1
                active.pop(page_key, None)
        elif kind == "access":
            state = active.get(page_key)
            add_lane_event(page_key, ts, "access", pid)
            if state is None:
                anomalies["access_without_active_add"] += 1
            else:
                state["segmentAccessCount"] = int(state.get("segmentAccessCount", 0)) + 1
                if pid != state["pidName"]:
                    close_lane_segment(page_key, ts, "pid_change")
                    by_pid[state["pidName"]] -= 1
                    by_pid[pid] += 1
                    by_file_pid[make_file_pid_key(state["fileKey"], state["pidName"])] -= 1
                    by_file_pid[make_file_pid_key(state["fileKey"], pid)] += 1
                    state["pidName"] = pid
                    state["segmentStart"] = ts

    while next_bucket <= end_ts + 1e-9:
        emit_snapshot(next_bucket)
        next_bucket += bucket_seconds

    for page_key in list(active.keys()):
        if page_key in lane_order:
            close_lane_segment(page_key, end_ts, "still_resident_at_end")

    file_meta = {}
    file_keys_from_file_pid = {split_file_pid_key(key)[0] for key in top_file_pids if key != "__other__"}
    for key in set(top_files) | file_keys_from_file_pid | {lane["fileKey"] for lane in lanes.values()}:
        if key == "__other__":
            continue
        dev, ino = split_file_key(key)
        meta = mapping.get((dev, ino), {})
        file_meta[key] = {
            "dev": dev,
            "ino": ino,
            "filename": meta.get("filename") or key,
            "size": meta.get("size", 0),
        }

    def file_pid_label(key: str) -> str:
        if key == "__other__":
            return "Other file + pid_name"
        file_key, pid = split_file_pid_key(key)
        file_name = file_meta.get(file_key, {}).get("filename", file_key)
        return f"{file_name} · {pid or 'unknown'}"

    ordered_lanes = [
        lanes[key]
        for key in sorted(lanes.keys(), key=lambda item: lane_order.get(item, 10**12))
    ]
    return {
        "metadata": {
            "dbPath": os.path.abspath(args.db),
            "start": start_ts,
            "end": end_ts,
            "bucketSeconds": bucket_seconds,
            "pageSize": PAGE_SIZE,
            "includeAccess": args.include_access,
            "eventLimit": args.event_limit,
            "totalEvents": total_events,
            "openPagesAtEnd": len(active),
            "anomalies": dict(anomalies),
        },
        "groups": {
            "files": [{"key": k, "label": file_meta.get(k, {}).get("filename", k)} for k in top_files]
            + ([{"key": "__other__", "label": "Other files"}] if any("__other__" in s["byFile"] for s in series) else []),
            "pids": [{"key": k, "label": k} for k in top_pids]
            + ([{"key": "__other__", "label": "Other pid_name"}] if any("__other__" in s["byPid"] for s in series) else []),
            "filePids": [{"key": k, "label": file_pid_label(k)} for k in top_file_pids]
            + ([{"key": "__other__", "label": "Other file + pid_name"}] if any("__other__" in s["byFilePid"] for s in series) else []),
        },
        "fileMeta": file_meta,
        "series": series,
        "lanes": ordered_lanes,
        "timesteps": load_timesteps(conn, start_ts, end_ts),
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>File Page Cache Residency</title>
<style>
:root {
  color-scheme: light;
  --ink: #16211f;
  --muted: #66706d;
  --line: #cfd8d3;
  --panel: #f7f4ee;
  --paper: #fffdf8;
  --accent: #0f766e;
  --danger: #b42318;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: ui-sans-serif, "Avenir Next", "Helvetica Neue", Helvetica, sans-serif;
}
header {
  padding: 20px 24px 12px;
  border-bottom: 1px solid var(--line);
  background: #f2efe8;
  position: sticky;
  top: 0;
  z-index: 10;
}
h1 {
  margin: 0 0 12px;
  font-size: 22px;
  line-height: 1.2;
  letter-spacing: 0;
}
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 16px;
  align-items: center;
}
.segmented {
  display: inline-grid;
  grid-auto-flow: column;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--line);
  background: #fffaf0;
  border-radius: 8px;
}
button {
  appearance: none;
  border: 0;
  background: transparent;
  color: var(--muted);
  padding: 8px 11px;
  min-width: 76px;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
}
button.active {
  background: var(--ink);
  color: white;
}
input[type="search"] {
  width: min(420px, 100%);
  border: 1px solid var(--line);
  background: white;
  border-radius: 7px;
  padding: 9px 11px;
  font-size: 13px;
}
main { padding: 18px 24px 28px; }
.stats {
  display: grid;
  grid-template-columns: repeat(5, minmax(130px, 1fr));
  gap: 10px;
  margin-bottom: 18px;
}
.stat {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 12px;
  background: #fbfaf6;
}
.stat b { display: block; font-size: 18px; margin-top: 2px; }
.stat span { color: var(--muted); font-size: 12px; }
.canvas-wrap {
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  background: white;
  margin-bottom: 16px;
}
.canvas-title {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: baseline;
  padding: 11px 13px;
  border-bottom: 1px solid var(--line);
  background: #fbfaf6;
}
.canvas-title strong { font-size: 14px; }
.canvas-title small { color: var(--muted); }
canvas {
  display: block;
  width: 100%;
}
#summaryCanvas { height: 260px; }
#lifeCanvas { height: 620px; }
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 12px;
  margin: 10px 0 18px;
  font-size: 12px;
}
.swatch {
  width: 11px;
  height: 11px;
  border-radius: 3px;
  display: inline-block;
  margin-right: 5px;
  vertical-align: -1px;
}
.tip {
  position: fixed;
  display: none;
  pointer-events: none;
  max-width: 520px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: rgba(255, 253, 248, .97);
  box-shadow: 0 8px 24px rgba(22, 33, 31, .13);
  font-size: 12px;
  z-index: 20;
}
.muted { color: var(--muted); }
@media (max-width: 760px) {
  header, main { padding-left: 14px; padding-right: 14px; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  #lifeCanvas { height: 520px; }
}
</style>
</head>
<body>
<header>
  <h1>File Page Cache Residency</h1>
  <div class="toolbar">
    <div class="segmented" aria-label="color mode">
      <button id="modeFile" class="active" type="button">文件颜色</button>
      <button id="modePid" type="button">pid_name 颜色</button>
      <button id="modeFilePid" type="button">文件+pid</button>
    </div>
    <input id="search" type="search" placeholder="过滤生命周期条带：filename / pid_name / dev:ino:ofs">
  </div>
</header>
<main>
  <section class="stats" id="stats"></section>
  <section class="canvas-wrap">
    <div class="canvas-title">
      <strong>整机 resident page 数量</strong>
      <small id="summaryLabel"></small>
    </div>
    <canvas id="summaryCanvas"></canvas>
  </section>
  <div class="legend" id="legend"></div>
  <section class="canvas-wrap">
    <div class="canvas-title">
      <strong>抽样文件页驻留生命周期</strong>
      <small>横轴为 timestamp；条带表示 resident，短线表示 add/access/delete</small>
    </div>
    <canvas id="lifeCanvas"></canvas>
  </section>
</main>
<div class="tip" id="tip"></div>
<script id="fscache-data" type="application/json">__DATA__</script>
<script>
const data = JSON.parse(document.getElementById('fscache-data').textContent);
let mode = 'file';
let filter = '';
const palette = [
  '#0f766e','#b42318','#1d4ed8','#ca8a04','#7c3aed','#15803d','#be185d',
  '#0e7490','#9a3412','#4338ca','#4d7c0f','#a21caf','#0369a1','#c2410c',
  '#166534','#6d28d9','#be123c','#155e75','#854d0e','#1e40af'
];

function keyColor(key) {
  if (key === '__other__') return '#9aa39f';
  let h = 2166136261;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return palette[Math.abs(h) % palette.length];
}
function fmt(n) { return new Intl.NumberFormat('en-US').format(Math.round(n || 0)); }
function timeFmt(t) { return Number(t).toFixed(3) + 's'; }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function resizeCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: rect.width, h: rect.height };
}
function filePidKey(fileKey, pidName) {
  return `${fileKey}\u001f${pidName || 'unknown'}`;
}
function groupList() {
  if (mode === 'file') return data.groups.files;
  if (mode === 'pid') return data.groups.pids;
  return data.groups.filePids || [];
}
function seriesBucket(s) {
  if (mode === 'file') return s.byFile;
  if (mode === 'pid') return s.byPid;
  return s.byFilePid || {};
}
function modeLabel() {
  if (mode === 'file') return '按文件分组';
  if (mode === 'pid') return '按 pid_name 分组';
  return '按文件 + pid_name 分组';
}
function labelForKey(key) {
  const found = groupList().find(g => g.key === key);
  return found ? found.label : key;
}
function installStats() {
  const m = data.metadata;
  const span = Math.max(0, m.end - m.start);
  const peak = Math.max(...data.series.map(s => s.total), 0);
  const anomalies = Object.values(m.anomalies || {}).reduce((a,b) => a + b, 0);
  const stats = [
    ['时间跨度', span.toFixed(3) + 's'],
    ['事件数', fmt(m.totalEvents)],
    ['峰值 resident pages', fmt(peak)],
    ['结束仍驻留', fmt(m.openPagesAtEnd)],
    ['异常事件', fmt(anomalies)]
  ];
  document.getElementById('stats').innerHTML = stats.map(([k,v]) =>
    `<div class="stat"><span>${k}</span><b>${v}</b></div>`).join('');
}
function drawSummary() {
  const canvas = document.getElementById('summaryCanvas');
  const {ctx, w, h} = resizeCanvas(canvas);
  const pad = {l: 54, r: 16, t: 16, b: 30};
  const iw = w - pad.l - pad.r;
  const ih = h - pad.t - pad.b;
  const start = data.metadata.start;
  const end = data.metadata.end;
  const maxY = Math.max(1, ...data.series.map(s => s.total));
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = '#d9e0dc';
  ctx.lineWidth = 1;
  ctx.font = '12px "Avenir Next", sans-serif';
  ctx.fillStyle = '#66706d';
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + ih * i / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    const value = maxY * (1 - i / 4);
    ctx.fillText(fmt(value), 8, y + 4);
  }
  for (const step of data.timesteps || []) {
    const x = pad.l + ((step.timestamp - start) / Math.max(end - start, .001)) * iw;
    if (x < pad.l || x > w - pad.r) continue;
    ctx.strokeStyle = '#7b8790';
    ctx.globalAlpha = .28;
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, h - pad.b); ctx.stroke();
    ctx.globalAlpha = 1;
  }
  const groups = groupList().map(g => g.key);
  const bases = new Array(data.series.length).fill(0);
  window.__summaryChart = { pad, w, h, iw, ih, start, end, maxY };
  for (const key of groups) {
    ctx.beginPath();
    for (let i = 0; i < data.series.length; i++) {
      const s = data.series[i];
      const x = pad.l + ((s.t - start) / Math.max(end - start, .001)) * iw;
      const y = pad.t + ih * (1 - (bases[i] + (seriesBucket(s)[key] || 0)) / maxY);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    for (let i = data.series.length - 1; i >= 0; i--) {
      const s = data.series[i];
      const x = pad.l + ((s.t - start) / Math.max(end - start, .001)) * iw;
      const y = pad.t + ih * (1 - bases[i] / maxY);
      ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.fillStyle = keyColor(key);
    ctx.globalAlpha = key === '__other__' ? .33 : .72;
    ctx.fill();
    ctx.globalAlpha = 1;
    for (let i = 0; i < data.series.length; i++) {
      bases[i] += seriesBucket(data.series[i])[key] || 0;
    }
  }
  ctx.strokeStyle = '#16211f';
  ctx.beginPath(); ctx.moveTo(pad.l, pad.t); ctx.lineTo(pad.l, h - pad.b); ctx.lineTo(w - pad.r, h - pad.b); ctx.stroke();
  ctx.fillStyle = '#66706d';
  ctx.fillText(timeFmt(start), pad.l, h - 9);
  ctx.textAlign = 'right';
  ctx.fillText(timeFmt(end), w - pad.r, h - 9);
  ctx.textAlign = 'left';
  document.getElementById('summaryLabel').textContent = modeLabel();
}
function filteredLanes() {
  const q = filter.trim().toLowerCase();
  if (!q) return data.lanes || [];
  return (data.lanes || []).filter(l => {
    const hay = [
      l.id, l.fileLabel, l.fileKey,
      ...(l.segments || []).map(s => s.pidName)
    ].join(' ').toLowerCase();
    return hay.includes(q);
  });
}
function drawLife() {
  const canvas = document.getElementById('lifeCanvas');
  const {ctx, w, h} = resizeCanvas(canvas);
  const lanes = filteredLanes();
  const start = data.metadata.start;
  const end = data.metadata.end;
  const pad = {l: 210, r: 18, t: 16, b: 30};
  const rowH = Math.max(10, Math.min(24, (h - pad.t - pad.b) / Math.max(lanes.length, 1)));
  const visible = Math.min(lanes.length, Math.floor((h - pad.t - pad.b) / rowH));
  const iw = w - pad.l - pad.r;
  window.__hitSegments = [];
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, w, h);
  ctx.font = '11px "Avenir Next", sans-serif';
  ctx.textBaseline = 'middle';
  for (const step of data.timesteps || []) {
    const x = pad.l + ((step.timestamp - start) / Math.max(end - start, .001)) * iw;
    if (x < pad.l || x > w - pad.r) continue;
    ctx.strokeStyle = '#7b8790';
    ctx.globalAlpha = .22;
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, h - pad.b); ctx.stroke();
    ctx.globalAlpha = 1;
  }
  for (let i = 0; i < visible; i++) {
    const lane = lanes[i];
    const y = pad.t + i * rowH;
    if (i % 2 === 0) {
      ctx.fillStyle = '#fbfaf6';
      ctx.fillRect(0, y, w, rowH);
    }
    ctx.fillStyle = '#3d4744';
    const label = `${lane.fileLabel} @${lane.ofs}`;
    ctx.fillText(label.length > 34 ? label.slice(0, 31) + '...' : label, 10, y + rowH / 2);
    for (const seg of lane.segments || []) {
      const x1 = pad.l + ((seg.start - start) / Math.max(end - start, .001)) * iw;
      const x2 = pad.l + ((seg.end - start) / Math.max(end - start, .001)) * iw;
      const colorKey = mode === 'file' ? seg.fileKey : (mode === 'pid' ? seg.pidName : filePidKey(seg.fileKey, seg.pidName));
      ctx.fillStyle = keyColor(colorKey);
      ctx.globalAlpha = .86;
      ctx.fillRect(Math.max(pad.l, x1), y + 2, Math.max(1, x2 - x1), Math.max(4, rowH - 4));
      ctx.globalAlpha = 1;
      window.__hitSegments.push({x1, x2, y1: y, y2: y + rowH, lane, seg});
    }
    for (const ev of lane.events || []) {
      const x = pad.l + ((ev.t - start) / Math.max(end - start, .001)) * iw;
      if (x < pad.l || x > w - pad.r) continue;
      ctx.strokeStyle = ev.kind === 'delete' ? '#b42318' : (ev.kind === 'add' ? '#16211f' : '#ffffff');
      ctx.lineWidth = ev.kind === 'access' ? 1 : 2;
      ctx.beginPath();
      ctx.moveTo(x, y + 2);
      ctx.lineTo(x, y + rowH - 2);
      ctx.stroke();
    }
  }
  ctx.strokeStyle = '#16211f';
  ctx.beginPath(); ctx.moveTo(pad.l, pad.t); ctx.lineTo(pad.l, h - pad.b); ctx.lineTo(w - pad.r, h - pad.b); ctx.stroke();
  ctx.fillStyle = '#66706d';
  ctx.textBaseline = 'alphabetic';
  ctx.fillText(timeFmt(start), pad.l, h - 9);
  ctx.textAlign = 'right';
  ctx.fillText(timeFmt(end), w - pad.r, h - 9);
  ctx.textAlign = 'left';
  if (!lanes.length) {
    ctx.fillStyle = '#66706d';
    ctx.font = '14px "Avenir Next", sans-serif';
    ctx.fillText('没有匹配的生命周期条带', 18, 48);
  }
}
function drawLegend() {
  const groups = groupList().slice(0, 28);
  document.getElementById('legend').innerHTML = groups.map(g =>
    `<span title="${escapeHtml(g.label)}"><i class="swatch" style="background:${keyColor(g.key)}"></i>${escapeHtml(g.label)}</span>`
  ).join('');
}
function redraw() { drawSummary(); drawLegend(); drawLife(); }
function setMode(nextMode) {
  mode = nextMode;
  document.getElementById('modeFile').classList.toggle('active', mode === 'file');
  document.getElementById('modePid').classList.toggle('active', mode === 'pid');
  document.getElementById('modeFilePid').classList.toggle('active', mode === 'filePid');
  redraw();
}
document.getElementById('modeFile').onclick = () => {
  setMode('file');
};
document.getElementById('modePid').onclick = () => {
  setMode('pid');
};
document.getElementById('modeFilePid').onclick = () => {
  setMode('filePid');
};
document.getElementById('search').oninput = (e) => { filter = e.target.value; drawLife(); };
document.getElementById('summaryCanvas').addEventListener('mousemove', (e) => {
  const tip = document.getElementById('tip');
  const chart = window.__summaryChart;
  if (!chart || !data.series?.length) { tip.style.display = 'none'; return; }
  const rect = e.currentTarget.getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  const {pad, iw, ih, start, end, maxY} = chart;
  if (x < pad.l || x > rect.width - pad.r || y < pad.t || y > rect.height - pad.b) {
    tip.style.display = 'none';
    return;
  }
  const t = start + ((x - pad.l) / Math.max(iw, 1)) * Math.max(end - start, .001);
  let index = 0;
  let best = Infinity;
  for (let i = 0; i < data.series.length; i++) {
    const delta = Math.abs(data.series[i].t - t);
    if (delta < best) { best = delta; index = i; }
  }
  const s = data.series[index];
  const targetValue = maxY * (1 - (y - pad.t) / Math.max(ih, 1));
  let base = 0;
  let hitKey = null;
  let hitCount = 0;
  for (const g of groupList()) {
    const count = seriesBucket(s)[g.key] || 0;
    if (count > 0 && targetValue >= base && targetValue <= base + count) {
      hitKey = g.key;
      hitCount = count;
      break;
    }
    base += count;
  }
  const label = hitKey ? labelForKey(hitKey) : 'total resident pages';
  const pct = s.total ? (hitCount * 100 / s.total).toFixed(1) : '0.0';
  tip.innerHTML = `<b>${escapeHtml(modeLabel())}</b><br>` +
    `<span class="muted">${timeFmt(s.t)} · bucket ${index + 1}/${data.series.length}</span><br>` +
    `${escapeHtml(label)}<br>` +
    `pages: <b>${fmt(hitKey ? hitCount : s.total)}</b>` +
    (hitKey ? ` · total: ${fmt(s.total)} · ${pct}%` : '');
  tip.style.left = Math.min(window.innerWidth - 540, e.clientX + 14) + 'px';
  tip.style.top = Math.min(window.innerHeight - 120, e.clientY + 14) + 'px';
  tip.style.display = 'block';
});
document.getElementById('summaryCanvas').addEventListener('mouseleave', () => {
  document.getElementById('tip').style.display = 'none';
});
document.getElementById('lifeCanvas').addEventListener('mousemove', (e) => {
  const tip = document.getElementById('tip');
  const rect = e.currentTarget.getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  const hit = (window.__hitSegments || []).find(s => x >= s.x1 && x <= s.x2 && y >= s.y1 && y <= s.y2);
  if (!hit) { tip.style.display = 'none'; return; }
  tip.innerHTML = `<b>${escapeHtml(hit.lane.fileLabel)}</b><br>` +
    `<span class="muted">${escapeHtml(hit.lane.id)}</span><br>` +
    `pid_name: ${escapeHtml(hit.seg.pidName)}<br>` +
    `${timeFmt(hit.seg.start)} - ${timeFmt(hit.seg.end)} · access ${fmt(hit.seg.accessCount)}`;
  tip.style.left = Math.min(window.innerWidth - 540, e.clientX + 14) + 'px';
  tip.style.top = Math.min(window.innerHeight - 120, e.clientY + 14) + 'px';
  tip.style.display = 'block';
});
document.getElementById('lifeCanvas').addEventListener('mouseleave', () => {
  document.getElementById('tip').style.display = 'none';
});
window.addEventListener('resize', redraw);
installStats();
redraw();
</script>
</body>
</html>
"""


def write_outputs(data: Dict[str, object], html_path: str, json_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(html_path)) or ".", exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    embedded = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    page = HTML_TEMPLATE.replace("__DATA__", embedded)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconstruct and visualize file page cache residency lifecycles."
    )
    parser.add_argument("--db", required=True, help="Path to the ftrace SQLite DB.")
    parser.add_argument(
        "--html",
        default="fscache_residency.html",
        help="Standalone HTML output path.",
    )
    parser.add_argument(
        "--json",
        default="fscache_residency.json",
        help="JSON output path.",
    )
    parser.add_argument("--start", type=float, help="Start timestamp filter.")
    parser.add_argument("--end", type=float, help="End timestamp filter.")
    parser.add_argument(
        "--bucket-seconds",
        type=float,
        help="Aggregation bucket width. Default auto-targets about 260 points.",
    )
    parser.add_argument(
        "--target-points",
        type=int,
        default=260,
        help="Target number of aggregate points when bucket width is automatic.",
    )
    parser.add_argument(
        "--max-groups",
        type=int,
        default=24,
        help="Top files, pid_name, and file+pid_name groups to color in the aggregate chart.",
    )
    parser.add_argument(
        "--max-lanes",
        type=int,
        default=700,
        help="Number of page lifecycles to retain for the lower chart.",
    )
    parser.add_argument(
        "--max-lane-events",
        type=int,
        default=2000,
        help="Maximum add/access/delete tick marks per retained page lane.",
    )
    parser.add_argument(
        "--pid-like",
        help="Optional SQL LIKE filter for pid_name, for example 'RecordTrace%%'.",
    )
    parser.add_argument(
        "--file-like",
        help="Optional SQL LIKE filter for inode_mapping.filename, for example '%%/data/%%'.",
    )
    parser.add_argument(
        "--event-limit",
        type=int,
        help="Debug/testing limit after timestamp ordering.",
    )
    parser.add_argument(
        "--no-access",
        dest="include_access",
        action="store_false",
        help="Ignore access events. Add/delete still reconstruct residency.",
    )
    parser.set_defaults(include_access=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    conn = connect(args.db)
    require_tables(conn, args.include_access)
    if args.file_like and not table_exists(conn, INODE_TABLE):
        raise SystemExit("--file-like requires inode_mapping table.")

    print("Loading inode mapping...", file=sys.stderr)
    mapping = load_inode_mapping(conn)

    print("Scanning timestamp range...", file=sys.stderr)
    start_ts, end_ts = min_max_timestamp(
        conn,
        args.include_access,
        args.start,
        args.end,
        args.pid_like,
        args.file_like,
        args.event_limit,
    )
    bucket_seconds = choose_bucket_seconds(
        start_ts, end_ts, args.bucket_seconds, args.target_points
    )
    print(
        f"Range {start_ts:.6f}..{end_ts:.6f}, bucket={bucket_seconds:.6f}s",
        file=sys.stderr,
    )

    print("Selecting lifecycle lanes...", file=sys.stderr)
    candidate_pages = load_candidate_pages(
        conn, args.max_lanes, args.start, args.end, args.pid_like, args.file_like
    )

    print("Pass 1/2: finding peak resident groups...", file=sys.stderr)
    top_files, top_pids, top_file_pids, peak_stats = pass_for_peaks(
        conn, args, start_ts, end_ts, bucket_seconds, mapping
    )
    print(
        f"Top files={len(top_files)}, top pid_name={len(top_pids)}, "
        f"top file+pid_name={len(top_file_pids)}, "
        f"events={peak_stats['totalEvents']}",
        file=sys.stderr,
    )

    print("Pass 2/2: reconstructing series and page lifecycles...", file=sys.stderr)
    data = reconstruct(
        conn,
        args,
        start_ts,
        end_ts,
        bucket_seconds,
        mapping,
        top_files,
        top_pids,
        top_file_pids,
        candidate_pages,
    )
    write_outputs(data, args.html, args.json)
    print(f"Wrote {os.path.abspath(args.html)}", file=sys.stderr)
    print(f"Wrote {os.path.abspath(args.json)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
