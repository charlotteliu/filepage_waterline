#!/usr/bin/env python3
"""
Generate a deterministic, larger synthetic ftrace-style file page cache DB.

The dataset models ten representative high-frequency mobile app scenarios:
messaging, short video, shopping, payment, maps, music, video, food delivery,
social browsing, and gaming. It is not a real popularity ranking; the goal is to
stress the residency visualizer with realistic-looking add/access/delete shapes.
"""

import argparse
import collections
import os
import random
import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple


PAGE_SIZE = 4096
TRACE_END = 3300.0


@dataclass(frozen=True)
class AppProfile:
    name: str
    package: str
    scene: str
    start: float
    duration: float
    hot_pages: int
    churn_pages: int
    db_pages: int
    media_pages: int
    background_pages: int


APPS = [
    AppProfile("WeChat", "com.tencent.mm", "消息列表、图片预览、小程序切换", 80, 420, 850, 1600, 420, 750, 260),
    AppProfile("Douyin", "com.ss.android.ugc.aweme", "短视频冷启动、连续刷视频、预加载", 260, 520, 780, 2800, 220, 3600, 180),
    AppProfile("Taobao", "com.taobao.taobao", "首页推荐、搜索、详情页、图片缓存", 520, 430, 720, 1700, 280, 1900, 170),
    AppProfile("Alipay", "com.eg.android.AlipayGphone", "扫码、支付页、账单查询", 760, 300, 610, 780, 360, 280, 220),
    AppProfile("Meituan", "com.sankuai.meituan", "外卖首页、商家列表、订单提交", 980, 380, 640, 1250, 330, 1150, 180),
    AppProfile("Amap", "com.autonavi.minimap", "路线规划、地图瓦片、导航后台", 1210, 540, 700, 1350, 210, 2650, 360),
    AppProfile("HuaweiMusic", "com.huawei.music", "播放列表、在线音乐、后台播放", 1540, 500, 560, 920, 180, 1850, 420),
    AppProfile("Bilibili", "tv.danmaku.bili", "视频详情、弹幕、连续播放", 1840, 520, 730, 1900, 260, 3100, 220),
    AppProfile("JD", "com.jingdong.app.mall", "首页、搜索、商品详情、结算", 2180, 410, 680, 1450, 300, 1600, 160),
    AppProfile("Xiaohongshu", "com.xingin.xhs", "信息流、图文笔记、评论滑动", 2420, 500, 740, 2100, 260, 2400, 190),
]


SCHEMA = """
CREATE TABLE inode_mapping
        (ino TEXT PRIMARY KEY, dev TEXT, size INTEGER, filename TEXT);
CREATE TABLE mm_filemap_access_history
        (event_type TEXT, dev TEXT, ino TEXT, page TEXT, pfn INTEGER, ofs INTEGER, mmapcnt INTEGER, timestamp REAL, pid INTEGER, pid_name TEXT);
CREATE TABLE mm_filemap_add_to_page_cache
        (dev TEXT, ino TEXT, page TEXT, pfn INTEGER, ofs INTEGER, mmapcnt INTEGER, flags TEXT, timestamp REAL, pid INTEGER, pid_name TEXT);
CREATE TABLE mm_filemap_delete_from_page_cache
        (dev TEXT, ino TEXT, page TEXT, pfn INTEGER, ofs INTEGER, mmapcnt INTEGER, flags TEXT, timestamp REAL, pid INTEGER, pid_name TEXT);
CREATE TABLE mm_filemap_label_page_cache
        (dev TEXT, ino TEXT, page TEXT, pfn INTEGER, ofs INTEGER, mmapcnt INTEGER, label INTEGER, accessbit INTEGER, timestamp REAL, pid INTEGER, pid_name TEXT);
CREATE TABLE result_adddel_only_mmapcnt0_access0(
  dev TEXT,
  ino TEXT,
  ofs INT,
  add_del_count,
  add_count,
  delete_count,
  access_count,
  add_del_mmapcnt0_count,
  add_del_bad_mmapcnt_count,
  first_ts,
  last_ts,
  page_type
);
CREATE TABLE timestep
        (timestamp REAL PRIMARY KEY, step TEXT);
CREATE TABLE tracing_mark_fabit
        (dev TEXT, ino TEXT, ofs INTEGER, timestamp REAL, pid INTEGER, pid_name TEXT);
"""


INDEXES = """
CREATE INDEX idx_mm_filemap_add_to_page_cache_ts ON mm_filemap_add_to_page_cache(timestamp);
CREATE INDEX idx_mm_filemap_add_to_page_cache_dev_ino_ts ON mm_filemap_add_to_page_cache(dev, ino, timestamp);
CREATE INDEX idx_add_page_mmapcnt ON mm_filemap_add_to_page_cache(dev, ino, ofs, mmapcnt);
CREATE INDEX idx_mm_filemap_delete_from_page_cache_ts ON mm_filemap_delete_from_page_cache(timestamp);
CREATE INDEX idx_mm_filemap_delete_from_page_cache_dev_ino_ts ON mm_filemap_delete_from_page_cache(dev, ino, timestamp);
CREATE INDEX idx_del_page_mmapcnt ON mm_filemap_delete_from_page_cache(dev, ino, ofs, mmapcnt);
CREATE INDEX idx_access_page ON mm_filemap_access_history(dev, ino, ofs);
CREATE INDEX idx_access_ino_ofs ON mm_filemap_access_history(ino, ofs);
CREATE INDEX idx_fabit_ino_ofs ON tracing_mark_fabit(ino, ofs, timestamp);
CREATE INDEX idx_result_adddel_only_mmapcnt0_access0_count ON result_adddel_only_mmapcnt0_access0(add_del_count);
CREATE INDEX idx_result_adddel_only_mmapcnt0_access0_page ON result_adddel_only_mmapcnt0_access0(dev, ino, ofs);
"""


def page_addr(num: int) -> str:
    return f"0xffffff8{num:09x}"


def flags_for(kind: str, rng: random.Random) -> str:
    base = {
        "code": 0x1000410003,
        "db": 0x1000018003,
        "cache": 0x1001010003,
        "media": 0x401000410003,
        "background": 0x8001301010083,
    }[kind]
    return hex(base | rng.randrange(0, 0x80))


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(INDEXES)


def quantized(ts: float, rng: random.Random) -> float:
    return round(ts + rng.uniform(-0.035, 0.035), 6)


def build_file_catalog(
    rng: random.Random,
) -> Tuple[List[Tuple[str, str, int, str]], Dict[str, List[Tuple[str, str, int, str]]]]:
    rows: List[Tuple[str, str, int, str]] = []
    by_app: Dict[str, List[Tuple[str, str, int, str]]] = {}
    ino_counter = 0x50000
    devs = ["260:69", "260:70", "259:12", "0:45"]

    for app_index, app in enumerate(APPS):
        app_files: List[Tuple[str, str, int, str]] = []
        specs = [
            ("code", 10, app.hot_pages),
            ("db", 6, app.db_pages),
            ("cache", 14, app.churn_pages),
            ("media", 18, app.media_pages),
            ("background", 5, app.background_pages),
        ]
        for kind, count, total_pages in specs:
            remaining = total_pages
            for file_index in range(count):
                ino_counter += 1
                share = max(8, remaining // max(1, count - file_index))
                jitter = rng.randint(-share // 3, share // 2)
                pages = max(4, share + jitter)
                remaining = max(0, remaining - pages)
                dev = devs[(app_index + file_index) % len(devs)]
                filename = (
                    f"/data/app/{app.package}/{kind}/"
                    f"{app.package.split('.')[-1]}_{file_index:02d}.bin"
                )
                row = (hex(ino_counter), dev, pages * PAGE_SIZE, filename)
                rows.append(row)
                app_files.append((kind, row[0], row[1], pages))
        by_app[app.name] = app_files
    return rows, by_app


def pick_files(app_files: List[Tuple[str, str, int, str]], kind: str) -> List[Tuple[str, str, int, str]]:
    return [item for item in app_files if item[0] == kind]


def add_event(
    add_rows: List[Tuple[str, str, str, int, int, int, str, float, int, str]],
    summary: Dict[Tuple[str, str, int], Dict[str, float]],
    dev: str,
    ino: str,
    ofs: int,
    ts: float,
    pid: int,
    pid_name: str,
    kind: str,
    rng: random.Random,
    page_seq: int,
) -> int:
    pfn = 200000 + page_seq
    add_rows.append((dev, ino, page_addr(page_seq), pfn, ofs, 0, flags_for(kind, rng), ts, pid, pid_name))
    rec = summary.setdefault((dev, ino, ofs), {"add": 0, "delete": 0, "access": 0, "first": ts, "last": ts})
    rec["add"] += 1
    rec["first"] = min(rec["first"], ts)
    rec["last"] = max(rec["last"], ts)
    return page_seq + 1


def delete_event(
    delete_rows: List[Tuple[str, str, str, int, int, int, str, float, int, str]],
    summary: Dict[Tuple[str, str, int], Dict[str, float]],
    dev: str,
    ino: str,
    ofs: int,
    ts: float,
    pid: int,
    pid_name: str,
    kind: str,
    rng: random.Random,
    page_seq: int,
) -> int:
    pfn = 200000 + page_seq
    delete_rows.append((dev, ino, page_addr(page_seq), pfn, ofs, 0, flags_for(kind, rng), ts, pid, pid_name))
    rec = summary.setdefault((dev, ino, ofs), {"add": 0, "delete": 0, "access": 0, "first": ts, "last": ts})
    rec["delete"] += 1
    rec["first"] = min(rec["first"], ts)
    rec["last"] = max(rec["last"], ts)
    return page_seq + 1


def access_event(
    access_rows: List[Tuple[str, str, str, str, int, int, int, float, int, str]],
    summary: Dict[Tuple[str, str, int], Dict[str, float]],
    dev: str,
    ino: str,
    ofs: int,
    ts: float,
    pid: int,
    pid_name: str,
    rng: random.Random,
    page_seq: int,
) -> int:
    pfn = 200000 + page_seq
    access_rows.append(("mm_filemap_mark_reaccess", dev, ino, page_addr(page_seq), pfn, ofs, 0, ts, pid, pid_name))
    rec = summary.setdefault((dev, ino, ofs), {"add": 0, "delete": 0, "access": 0, "first": ts, "last": ts})
    rec["access"] += 1
    rec["first"] = min(rec["first"], ts)
    rec["last"] = max(rec["last"], ts)
    return page_seq + 1


def generate_events(
    by_app: Dict[str, List[Tuple[str, str, int, str]]],
    rng: random.Random,
):
    add_rows = []
    delete_rows = []
    access_rows = []
    label_rows = []
    timestep_rows = []
    summary: Dict[Tuple[str, str, int], Dict[str, float]] = {}
    page_seq = 1
    pid_base = 1800

    for app_index, app in enumerate(APPS):
        pid = pid_base + app_index * 37
        reclaim_pid = 33
        app_files = by_app[app.name]
        timestep_rows.extend(
            [
                (round(app.start, 3), f"{app.name}: 启动 - {app.scene}"),
                (round(app.start + app.duration * 0.22, 3), f"{app.name}: 前台高频访问"),
                (round(app.start + app.duration * 0.62, 3), f"{app.name}: 退后台/缓存保活"),
                (round(app.start + app.duration * 0.88, 3), f"{app.name}: 系统回收冷页"),
            ]
        )

        active_pages: List[Tuple[str, str, int, str, float, bool]] = []
        active_until: Dict[Tuple[str, str, int], float] = {}

        def reserve_lifecycle(
            dev: str,
            ino: str,
            ofs: int,
            load_ts: float,
            delete_ts: float,
            should_delete: bool,
        ) -> Tuple[float, float, bool]:
            key = (dev, ino, ofs)
            previous_end = active_until.get(key)
            if previous_end is not None and load_ts <= previous_end:
                load_ts = quantized(previous_end + rng.uniform(0.8, 4.5), rng)
                delete_ts = max(delete_ts, load_ts + rng.uniform(8, 70))
            if load_ts > TRACE_END:
                return load_ts, delete_ts, False
            active_until[key] = delete_ts if should_delete else TRACE_END
            return load_ts, delete_ts, True

        # Long-lived code and database pages: load fast, remain through most of the scenario.
        for kind in ("code", "db", "background"):
            files = pick_files(app_files, kind)
            for file_kind, ino, dev, pages in files:
                selected = min(pages, max(6, int(pages * rng.uniform(0.35, 0.72))))
                for page_index in rng.sample(range(pages), selected):
                    ofs = page_index * PAGE_SIZE
                    load_ts = quantized(app.start + rng.expovariate(1 / 18), rng)
                    unload_bias = {"code": 0.92, "db": 0.78, "background": 1.12}[kind]
                    delete_ts = quantized(app.start + app.duration * unload_bias + rng.uniform(-20, 90), rng)
                    should_delete = rng.random() < (0.82 if kind in ("cache", "media") else 0.72)
                    load_ts, delete_ts, ok = reserve_lifecycle(dev, ino, ofs, load_ts, delete_ts, should_delete)
                    if not ok:
                        continue
                    page_seq = add_event(add_rows, summary, dev, ino, ofs, load_ts, pid, app.name, kind, rng, page_seq)
                    active_pages.append((dev, ino, ofs, kind, delete_ts, should_delete))
                    for _ in range(rng.randint(1, 5 if kind != "background" else 2)):
                        ats = quantized(load_ts + rng.uniform(3, max(6, min(app.duration * 0.75, delete_ts - load_ts))), rng)
                        actor = app.name if rng.random() > 0.08 else f"{app.name}:RenderThread"
                        page_seq = access_event(access_rows, summary, dev, ino, ofs, ats, pid, actor, rng, page_seq)

        # Churn cache pages: repeated bursts, many pages resident for only a short window.
        cache_files = pick_files(app_files, "cache")
        for burst in range(18):
            center = app.start + 35 + burst * (app.duration * 0.75 / 18) + rng.uniform(-8, 8)
            burst_pages = rng.randint(65, 150)
            for _ in range(burst_pages):
                kind, ino, dev, pages = rng.choice(cache_files)
                page_index = rng.randrange(pages)
                ofs = page_index * PAGE_SIZE
                load_ts = quantized(center + rng.uniform(-5, 9), rng)
                lifetime = rng.uniform(18, 95)
                delete_ts = quantized(min(app.start + app.duration + rng.uniform(25, 160), load_ts + lifetime), rng)
                should_delete = rng.random() < 0.82
                load_ts, delete_ts, ok = reserve_lifecycle(dev, ino, ofs, load_ts, delete_ts, should_delete)
                if not ok:
                    continue
                page_seq = add_event(add_rows, summary, dev, ino, ofs, load_ts, pid, app.name, kind, rng, page_seq)
                active_pages.append((dev, ino, ofs, kind, delete_ts, should_delete))
                for _ in range(rng.randint(0, 3)):
                    ats = quantized(load_ts + rng.uniform(0.2, max(0.4, delete_ts - load_ts)), rng)
                    page_seq = access_event(access_rows, summary, dev, ino, ofs, ats, pid, app.name, rng, page_seq)

        # Media-heavy pages: short video, map tiles, product images, album art, etc.
        media_files = pick_files(app_files, "media")
        media_bursts = 28 if app.media_pages > 2500 else 16
        for burst in range(media_bursts):
            center = app.start + 45 + burst * (app.duration * 0.82 / media_bursts) + rng.uniform(-6, 6)
            burst_pages = rng.randint(80, 220 if app.media_pages > 2000 else 120)
            for _ in range(burst_pages):
                kind, ino, dev, pages = rng.choice(media_files)
                page_index = rng.randrange(pages)
                ofs = page_index * PAGE_SIZE
                load_ts = quantized(center + rng.uniform(-4, 8), rng)
                lifetime = rng.uniform(8, 70)
                delete_ts = quantized(min(app.start + app.duration + rng.uniform(15, 110), load_ts + lifetime), rng)
                should_delete = rng.random() < 0.82
                load_ts, delete_ts, ok = reserve_lifecycle(dev, ino, ofs, load_ts, delete_ts, should_delete)
                if not ok:
                    continue
                page_seq = add_event(add_rows, summary, dev, ino, ofs, load_ts, pid, app.name, kind, rng, page_seq)
                active_pages.append((dev, ino, ofs, kind, delete_ts, should_delete))
                access_repeats = rng.randint(1, 5 if app.media_pages > 2000 else 3)
                for _ in range(access_repeats):
                    ats = quantized(load_ts + rng.uniform(0.1, max(0.2, delete_ts - load_ts)), rng)
                    actor = app.name if rng.random() > 0.14 else f"{app.name}:IO"
                    page_seq = access_event(access_rows, summary, dev, ino, ofs, ats, pid, actor, rng, page_seq)

        for dev, ino, ofs, kind, delete_ts, should_delete in active_pages:
            if should_delete:
                page_seq = delete_event(delete_rows, summary, dev, ino, ofs, delete_ts, reclaim_pid, "kswapd0", kind, rng, page_seq)
            else:
                # Some pages survive the trace window and show as still resident.
                label_rows.append((dev, ino, page_addr(page_seq), 200000 + page_seq, ofs, 0, 2, 1, quantized(delete_ts, rng), pid, app.name))
                page_seq += 1

    # A small global reclaim wave to make the whole-machine waterline visibly drop.
    timestep_rows.append((3020.0, "系统全局内存压力：后台缓存集中回收"))
    return add_rows, delete_rows, access_rows, label_rows, sorted(set(timestep_rows)), summary


def summary_rows(summary: Dict[Tuple[str, str, int], Dict[str, float]]):
    rows = []
    for (dev, ino, ofs), rec in summary.items():
        add_count = int(rec["add"])
        delete_count = int(rec["delete"])
        access_count = int(rec["access"])
        add_del_count = add_count + delete_count
        if add_count and delete_count and access_count:
            page_type = "synthetic_active_lifecycle"
        elif add_count and delete_count:
            page_type = "synthetic_add_delete_only"
        elif add_count:
            page_type = "synthetic_still_resident"
        else:
            page_type = "synthetic_observed_only"
        rows.append(
            (
                dev,
                ino,
                ofs,
                add_del_count,
                add_count,
                delete_count,
                access_count,
                add_del_count,
                0,
                round(rec["first"], 6),
                round(rec["last"], 6),
                page_type,
            )
        )
    rows.sort(key=lambda r: (r[3], r[6], r[10] - r[9]), reverse=True)
    return rows


def insert_many(conn: sqlite3.Connection, table: str, rows: Iterable[tuple]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    placeholders = ",".join(["?"] * len(rows[0]))
    conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
    return len(rows)


def generate_db(path: str, seed: int) -> Dict[str, int]:
    rng = random.Random(seed)
    if os.path.exists(path):
        os.remove(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
    create_schema(conn)

    inode_rows, by_app = build_file_catalog(rng)
    add_rows, delete_rows, access_rows, label_rows, timestep_rows, summary = generate_events(by_app, rng)

    counts = {}
    with conn:
        counts["inode_mapping"] = insert_many(conn, "inode_mapping", inode_rows)
        counts["mm_filemap_add_to_page_cache"] = insert_many(conn, "mm_filemap_add_to_page_cache", add_rows)
        counts["mm_filemap_delete_from_page_cache"] = insert_many(conn, "mm_filemap_delete_from_page_cache", delete_rows)
        counts["mm_filemap_access_history"] = insert_many(conn, "mm_filemap_access_history", access_rows)
        counts["mm_filemap_label_page_cache"] = insert_many(conn, "mm_filemap_label_page_cache", label_rows)
        counts["result_adddel_only_mmapcnt0_access0"] = insert_many(
            conn, "result_adddel_only_mmapcnt0_access0", summary_rows(summary)
        )
        counts["timestep"] = insert_many(conn, "timestep", timestep_rows)
    create_indexes(conn)
    conn.execute("VACUUM")
    conn.close()
    counts["distinct_pages"] = len(summary)
    counts["db_size_bytes"] = os.path.getsize(path)
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the top-apps demo fscache SQLite DB.")
    parser.add_argument(
        "--out",
        default="outputs/top_apps_fscache_demo.db",
        help="Output SQLite DB path.",
    )
    parser.add_argument("--seed", type=int, default=20260630, help="Deterministic RNG seed.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    counts = generate_db(args.out, args.seed)
    print(f"Wrote {os.path.abspath(args.out)}")
    for key in sorted(counts):
        print(f"{key}: {counts[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
