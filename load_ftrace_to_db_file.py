import re
import sqlite3
import os
import time
import zipfile
import zlib
import pandas as pd
from datetime import datetime
from script.full_life_zero_access_page import *
from script.page_origin_trace import *


PREFIX = (
    r'^\s*(?P<pid_name>.+?)-(?P<pid>\d+)\s+\(.*?\)[\s\t]+'
    r'\[(?P<cpu>\d+)\][\.\s\t]{4,}\s+(?P<timestamp>\d+\.\d+):\s+'
)

RE_ADD = re.compile(PREFIX +
    r'mm_filemap_add_to_page_cache:\s+dev\s+(?P<dev>\d+:\d+)\s+ino\s+(?P<ino>0x[0-9a-fA-F]+)\s+'
    r'page=(?P<page>0x[0-9a-fA-F]+)\s+pfn=(?P<pfn>\d+)\s+ofs=(?P<ofs>\d+)\s+mmapcnt=(?P<mmapcnt>\d+)\s+flags=(?P<flags>0x[0-9a-fA-F]+)')

RE_DEL = re.compile(PREFIX +
    r'mm_filemap_delete_from_page_cache:\s+dev\s+(?P<dev>\d+:\d+)\s+ino\s+(?P<ino>0x[0-9a-fA-F]+)\s+'
    r'page=(?P<page>0x[0-9a-fA-F]+)\s+pfn=(?P<pfn>\d+)\s+ofs=(?P<ofs>\d+)\s+mmapcnt=(?P<mmapcnt>\d+)\s+flags=(?P<flags>0x[0-9a-fA-F]+)')

RE_ACCESS = re.compile(PREFIX +
    r'(?P<event_type>mm_filemap_mark_access|mm_filemap_mark_reaccess|mm_filemap_mark_referenced):\s+'
    r'dev\s+(?P<dev>\d+:\d+)\s+ino\s+(?P<ino>0x[0-9a-fA-F]+)\s+page=(?P<page>0x[0-9a-fA-F]+)\s+'
    r'pfn=(?P<pfn>\d+)\s+ofs=(?P<ofs>\d+)\s+mmapcnt=(?P<mmapcnt>\d+)')

RE_FABIT = re.compile(PREFIX +
    r'tracing_mark_write:\s+B\|[^\|]+\|fabit\s+d=(?P<dev>\d+:\d+)\s+i=(?P<ino>\d+)\s+o=(?P<ofs>\d+)')

RE_LABEL = re.compile(PREFIX +
    r'mm_filemap_label_page_cache:\s+dev\s+(?P<dev>\d+:\d+)\s+ino\s+(?P<ino>0x[0-9a-fA-F]+)\s+'
    r'page=(?P<page>0x[0-9a-fA-F]+)\s+pfn=(?P<pfn>\d+)\s+ofs=(?P<ofs>\d+)\s+mmapcnt=(?P<mmapcnt>\d+)\s+'
    r'label=(?P<label>\d+)\s+accessbit=(?P<accessbit>\d+)')

# 匹配 bitmap 打点正则
RE_BITMAP = re.compile(
    PREFIX + r'tracing_mark_write:\s+B\|[^\|]+\|bitmap\s+'
    r'd=(?P<dev>\d+:\d+)\s+i=(?P<ino>\d+)\s+o=(?P<base_ofs>\d+)(?:\(rem\))?:\s+'
    r'(?P<bitmap_hex>[0-9a-fA-F]+)'
)

# ==========================================
# 用例步骤时间解析正则
# ==========================================
RE_STEP_TIME = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+(.+)')
RE_START_PCTIME = re.compile(r'pctime:(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})')

# ==========================================
# smaps 解析正则
# ==========================================
RE_SMAPS_SECTION = re.compile(
    r'^([0-9a-fA-F]+-[0-9a-fA-F]+)\s+'    # 地址范围
    r'[\-wrxp]+s?\s+'                    # 权限（含 s）
    r'([0-9a-fA-F]+)\s+'                 # offset
    r'([0-9a-fA-F]+:[0-9a-fA-F]+)\s+'    # dev
    r'(\d+)\s*'                          # ino
    r'(.*)',                             # 名称部分
    re.MULTILINE
)
RE_SMAPS_PSS = re.compile(r'^Pss:\s*(\d+)\s*kB', re.MULTILINE)
# RE_SMAPS_BUFTYPE = re.compile(r'buftype:\s*(\d+)\s+MappedSize:\s*(\d+)\s+AllZeroSize:\s*(\d+)\s+AccessedSize:\s*(\d*)', re.MULTILINE)
RE_SMAPS_BUFTYPE = re.compile(r'buftype:\s*(\d+)\s+MappedSize:\s*(\d+)\s+AllZeroSize:\s*(\d+)\s+mylabel1:\s*(\d+)\s+mylabel2:\s*(\d+)\s+AccessedSize:\s*(\d*)', re.MULTILINE)
RE_FILENAME_TIME = re.compile(r'(\d{8})_(\d{6})\.txt')

# ==========================================
# ARK 解析正则
# ==========================================
RE_ARK_LITERAL = re.compile(r'^\d+\s+(0x[0-9a-fA-F]+)\s+\{\s*\d+\s+\[(.*?)\]\}', re.MULTILINE)
RE_ARK_RECORD = re.compile(r'\.record\s+([^{]+?)\s*\{\s*#\s*offset:\s*(0x[0-9a-fA-F]+)(?:,\s*size:\s*(0x[0-9a-fA-F]+))?')
RE_ARK_METHOD = re.compile(r'\.function\s+([^{]+?)\s*\{[^#]*#\s*offset:\s*(0x[0-9a-fA-F]+)(?:,\s*code offset:\s*(0x[0-9a-fA-F]+))?')
RE_ARK_STRING = re.compile(r'\[offset:(0x[0-9a-fA-F]+),\s*name_value:(.*?)\]', re.DOTALL)


# ==========================================
# 2. 数据库初始化模块
# ==========================================
def init_database(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA cache_size = -64000")
    cursor = conn.cursor()

    tables = [
        "mm_filemap_add_to_page_cache", "mm_filemap_delete_from_page_cache",
        "tracing_mark_fabit", "mm_filemap_access_history",
        "mm_filemap_label_page_cache", "inode_mapping",
        "timestep",  "process_smaps", "ark_symbol_dump",
        "zeroaccess_page", "bitmap_page_info"

    ]
    for tbl in tables:
        cursor.execute(f'DROP TABLE IF EXISTS {tbl}')

    cursor.execute('''CREATE TABLE mm_filemap_add_to_page_cache
        (dev TEXT, ino TEXT, page TEXT, pfn INTEGER, ofs INTEGER, mmapcnt INTEGER, flags TEXT, timestamp REAL, pid INTEGER, pid_name TEXT)''')
    cursor.execute('''CREATE TABLE mm_filemap_delete_from_page_cache
        (dev TEXT, ino TEXT, page TEXT, pfn INTEGER, ofs INTEGER, mmapcnt INTEGER, flags TEXT, timestamp REAL, pid INTEGER, pid_name TEXT)''')
    cursor.execute('''CREATE TABLE tracing_mark_fabit
        (dev TEXT, ino TEXT, ofs INTEGER, timestamp REAL, pid INTEGER, pid_name TEXT)''')
    cursor.execute('''CREATE TABLE mm_filemap_access_history
        (event_type TEXT, dev TEXT, ino TEXT, page TEXT, pfn INTEGER, ofs INTEGER, mmapcnt INTEGER, timestamp REAL, pid INTEGER, pid_name TEXT)''')
    cursor.execute('''CREATE TABLE mm_filemap_label_page_cache
        (dev TEXT, ino TEXT, page TEXT, pfn INTEGER, ofs TEXT, mmapcnt INTEGER, label INTEGER, accessbit INTEGER, timestamp REAL, pid INTEGER, pid_name TEXT)''')
    cursor.execute('''CREATE TABLE inode_mapping
        (ino TEXT PRIMARY KEY, dev TEXT, size INTEGER, filename TEXT)''')

    cursor.execute('''CREATE TABLE timestep
        (timestamp REAL PRIMARY KEY, step TEXT)''')

    cursor.execute('''CREATE TABLE process_smaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        process_name TEXT,
        timestamp REAL,
        addr_name TEXT,
        addr_range TEXT,
        offset TEXT,
        dev TEXT,
        ino TEXT,
        pss INTEGER,
        buftype TEXT,
        MappedSize TEXT,
        AllZeroSize TEXT,
        mylabel1 TEXT,
        mylabel2 TEXT,
        AccessedSize TEXT
    )''')

    cursor.execute('''CREATE TABLE ark_symbol_dump (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        offset INTEGER NOT NULL,
        code_offset INTEGER,
        size INTEGER,
        type TEXT NOT NULL,
        name_value TEXT NOT NULL
    )''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_ark_offset ON ark_symbol_dump(offset)')

    # ===================== 【新增零访问页表】 =====================
    cursor.execute('''CREATE TABLE zeroaccess_page (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ino TEXT NOT NULL,
        filename TEXT,
        page_idx INTEGER,
        ofs_bytes INTEGER,
        ofs_hex TEXT,
        add_ts REAL,
        duration REAL,
        internal_file TEXT,
        internal_offset TEXT,
        compress_type TEXT
    )''')
    cursor.execute('CREATE INDEX idx_zero_ino ON zeroaccess_page(ino)')

    # 创建bitmap数据表
    cursor.execute('''CREATE TABLE bitmap_page_info (
        dev TEXT,
        ino TEXT,
        base_ofs INTEGER,
        page_ofs INTEGER,
        timestamp REAL,
        pid INTEGER,
        pid_name TEXT
    )''')

    conn.commit()
    return conn


# ==========================================
# 3. 解析inode信息 (Inode -> Filename)
# ==========================================
def load_inode_mapping(conn, mapping_file):
    if not os.path.exists(mapping_file):
        print(f"⚠️ 未找到映射文件 {mapping_file}")
        return

    print(f"[{time.strftime('%H:%M:%S')}] 开始导入 Inode 映射文件...")
    cursor = conn.cursor()
    buffer = []

    with open(mapping_file, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.lower().startswith(('ino', 'inode', '#')):
                continue

            parts = line.split(None, 3)
            if len(parts) >= 4:
                ino, dev, raw_size = parts[0], parts[1], parts[2]
                filename = parts[3].strip(' \t"')
                # #=======================
                # if ino.startswith("0x"):
                #     ino = ino[2:]
                # else:
                #     ino = ino
                # # =======================
                try:
                    size = int(raw_size)
                except ValueError:
                    size = 0
                    filename = f"[{raw_size}] {filename}"
                buffer.append((ino, dev, size, filename))

    cursor.executemany('INSERT OR IGNORE INTO inode_mapping VALUES (?,?,?,?)', buffer)
    conn.commit()
    print(f"✅ 成功导入 {len(buffer)} 条映射记录。")


# ==========================================
# 4.解析 stepinfo.txt
# ==========================================
def parse_step_info_to_db(conn, step_file):
    if not os.path.exists(step_file):
        print(f"⚠️ 未找到步骤文件: {step_file}")
        return
    print(f"\n[{time.strftime('%H:%M:%S')}] 开始解析用例步骤时间...")

    cursor = conn.cursor()
    step_data = []

    with open(step_file, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 匹配启动时间 pctime
        m_pc = RE_START_PCTIME.search(line)
        if m_pc:
            timestr = m_pc.group(1)
            try:
                dt = datetime.strptime(timestr, "%Y-%m-%d %H:%M:%S.%f")
                ts = round(dt.timestamp(), 3)
                step_data.append((ts, "用例启动时间"))
            except:
                continue

        # 匹配步骤行  2026-xx-xx xx:xx:xx.xxx 步骤描述
        m_step = RE_STEP_TIME.match(line)
        if m_step:
            timestr = m_step.group(1)
            desc = m_step.group(2).strip()
            try:
                dt = datetime.strptime(timestr, "%Y-%m-%d %H:%M:%S.%f")
                ts = round(dt.timestamp(), 3)
                step_data.append((ts, desc))
            except Exception as e:
                continue

    if step_data:
        cursor.executemany("INSERT OR REPLACE INTO timestep (timestamp, step) VALUES (?, ?)", step_data)
        conn.commit()
        print(f"✅ 步骤时间解析完成，共导入 {len(step_data)} 条步骤到 timestep 表")
    else:
        print("未解析到任何步骤时间")


# ==========================================
# 5.smaps信息解析
# ==========================================
def parse_smaps_folder_to_db(conn, root_smaps_path):
    if not os.path.isdir(root_smaps_path):
        print(f"⚠️ smaps 根目录不存在: {root_smaps_path}")
        return
    print(f"\n[{time.strftime('%H:%M:%S')}] 开始解析所有进程 smaps 数据...")
    cursor = conn.cursor()
    total_count = 0
    BATCH_INSERT = 200
    buffer = []

    process_dirs = [d for d in os.listdir(root_smaps_path) if os.path.isdir(os.path.join(root_smaps_path, d))]

    for proc_name in process_dirs:
        proc_path = os.path.join(root_smaps_path, proc_name)
        txt_files = [f for f in os.listdir(proc_path) if f.endswith('.txt')]

        for fn in txt_files:
            fp = os.path.join(proc_path, fn)
            if not RE_FILENAME_TIME.match(fn):
                continue

            try:
                date_str, time_str = RE_FILENAME_TIME.match(fn).groups()
                dt = datetime.strptime(f"{date_str} {time_str}", "%Y%m%d %H%M%S")
                ts = round(dt.timestamp(), 3)
            except:
                continue

            try:
                with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.read().splitlines()
            except:
                continue

            # ============================
            # 解析smaps打点信息
            # ============================
            current_block = []
            for line in lines:
                line = line.rstrip()
                # 新内存块开始：以 十六进制-十六进制 开头
                if re.match(r'^[0-9a-fA-F]+-[0-9a-fA-F]+ ', line):
                    if current_block:
                        block_text = '\n'.join(current_block)
                        # 处理上一个块
                        sec_match = RE_SMAPS_SECTION.search(block_text)
                        if sec_match:
                            addr_range = sec_match.group(1).strip()
                            offset = sec_match.group(2).strip()
                            dev = sec_match.group(3).strip()
                            ino = sec_match.group(4).strip()
                            name_part = sec_match.group(5).strip()

                            # =========================
                            # 包含 Size 就直接清空 addr_name
                            # =========================
                            if 'Size:' in name_part:
                                addr_name = ''
                            else:
                                addr_name = name_part if name_part else ''

                            # Pss
                            pss_val = 0
                            pss_match = RE_SMAPS_PSS.search(block_text)
                            if pss_match:
                                try:
                                    pss_val = int(pss_match.group(1))
                                except:
                                    pass

                            # buftype
                            bt_match = RE_SMAPS_BUFTYPE.search(block_text)
                            if bt_match:
                                bt, ms, azs, mylabel, mylabel2, asz = bt_match.groups()
                                buffer.append((
                                    proc_name, ts, addr_name, addr_range, offset,
                                    dev, ino, pss_val, bt, ms, azs, mylabel, mylabel2, asz or ''
                                ))
                                total_count += 1

                                if len(buffer) >= BATCH_INSERT:
                                    cursor.executemany('''
                                        INSERT INTO process_smaps (
                                            process_name, timestamp, addr_name, addr_range, offset,
                                            dev, ino, pss, buftype, MappedSize, AllZeroSize, mylabel1, mylabel2,
                                            AccessedSize
                                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                    ''', buffer)
                                    conn.commit()
                                    buffer.clear()

                    current_block = [line]
                else:
                    if current_block:
                        current_block.append(line)

            # 处理最后一个块
            if current_block:
                block_text = '\n'.join(current_block)
                sec_match = RE_SMAPS_SECTION.search(block_text)
                if sec_match:
                    addr_range = sec_match.group(1).strip()
                    offset = sec_match.group(2).strip()
                    dev = sec_match.group(3).strip()
                    ino = sec_match.group(4).strip()
                    name_part = sec_match.group(5).strip()

                    if 'Size:' in name_part:
                        addr_name = ''
                    else:
                        addr_name = name_part if name_part else ''

                    pss_val = 0
                    pss_match = RE_SMAPS_PSS.search(block_text)
                    if pss_match:
                        try:
                            pss_val = int(pss_match.group(1))
                        except:
                            pass

                    bt_match = RE_SMAPS_BUFTYPE.search(block_text)
                    if bt_match:
                        bt, ms, azs, mylabel, mylabel2, asz = bt_match.groups()
                        buffer.append((
                            proc_name, ts, addr_name, addr_range, offset,
                            dev, ino, pss_val, bt, ms, azs, mylabel, mylabel2, asz or ''
                        ))
                        total_count += 1

        if buffer:
            cursor.executemany('''
                INSERT INTO process_smaps (
                    process_name, timestamp, addr_name, addr_range, offset,
                    dev, ino, pss, buftype, MappedSize, AllZeroSize, mylabel1, mylabel2,AccessedSize
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', buffer)
            conn.commit()
            buffer.clear()

    print(f"✅ smaps 解析完成，共导入 {total_count} 条记录到 process_smaps 表")


# ==========================================
# 6.反汇编导入
# ==========================================
def parse_ark_to_db(conn, txt_file):
    if not os.path.exists(txt_file):
        print(f"⚠️ ARK 文件不存在: {txt_file}")
        return

    print(f"\n[{time.strftime('%H:%M:%S')}] 开始解析 ARK 反汇编文件 → ark_symbol_dump 表")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM ark_symbol_dump")
    insert_data = []

    with open(txt_file, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    # LITERALS
    # 0 0x15052cd { 1 [ string:"app.string.dialog_confirmed", ]} || index offsrt XX name_value
    for m in RE_ARK_LITERAL.finditer(content):
        off = int(m.group(1), 16)  # 16进制转10进制
        val = m.group(2).strip()
        insert_data.append((off, None, None, 'LITERAL', val))

    # RECORD
    # .record &@hw-hmos.abxconverter.index&1.0.1 { # offset: 0x4002a5, size: 0x009b (155)
    for m in RE_ARK_RECORD.finditer(content):
        name = m.group(1).strip()
        off = int(m.group(2), 16)
        size = int(m.group(3), 16) if m.group(3) else None  # 16进制转10进制
        insert_data.append((off, None, size, 'RECORD', name))

    # METHOD
    # .function any &sceneboard.src.main.ets.SceneBoardCard.pages.SceneBoard_2_1_Card&.#~b>#initialRender
    # (any a0, any a1, any a2) <static> { # offset: 0x04a7, code offset: 0x2206
    for m in RE_ARK_METHOD.finditer(content):
        name = m.group(1).strip()
        off = int(m.group(2), 16)
        code = int(m.group(3), 16) if m.group(3) else None
        insert_data.append((off, code, None, 'METHOD', name))

    # STRING
    # [offset:0xdeb, name_value:100%]
    for m in RE_ARK_STRING.finditer(content):
        off = int(m.group(1), 16)
        name = m.group(2).strip()
        insert_data.append((off, None, None, 'STRING', name))

    if insert_data:
        cursor.executemany('''
            INSERT INTO ark_symbol_dump (offset, code_offset, size, type, name_value)
            VALUES (?,?,?,?,?)
        ''', insert_data)
        conn.commit()
        print(f"✅ ARK 解析完成：共导入 {len(insert_data)} 条记录")
    else:
        print("⚠️ 未解析到任何 ARK 记录")


# ==========================================
# 分析零访问页并-支持指定 ino/全量分析
# ==========================================
def analyze_and_save_zero_access_pages(db_path, hap_path=None, ino=None, filename=None):
    """
    分析零访问页并入库
    :param db_path: 数据库路径
    :param hap_path: 可选 HAP 路径
    :param ino: 可选，指定分析某个 ino
    :param filename: 可选，指定文件名（和 ino 配对）
        不传 ino → 自动分析所有 inode
        传 ino → 只分析这一个
    """
    print(f"\n🔍 开始分析零访问页 → {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # ====================== 逻辑 ======================
    ino_list = []
    if ino is not None:
        # 指定单个 ino
        print(f"🎯 单个hap模式：仅分析 ino = {ino}, filename = {filename}")
        ino_list = [(ino, filename)]
    else:
        # 自动模式：读取所有文件
        print(f"🔁 自动模式：分析所有文件")
        cur.execute('''
            SELECT ino, filename FROM inode_mapping
        ''')
        ino_list = cur.fetchall()

    if not ino_list:
        print("⚠️ 无任何 inode 数据")
        conn.close()
        return

    # 初始化 HAP 解析器
    mapper = HapOffsetMapper(hap_path) if (hap_path and os.path.exists(hap_path)) else None
    total_insert = 0

    for curr_ino, curr_filename in ino_list:
        curr_filename = curr_filename or f"unknown_{curr_ino}"

        query = '''
        WITH AllAccess AS (
            SELECT (ofs * 4096) as ofs_bytes, timestamp FROM tracing_mark_fabit WHERE ino = ?
            UNION ALL
            SELECT ofs, timestamp FROM mm_filemap_access_history WHERE ino = ?
            UNION ALL
            SELECT ofs, timestamp FROM mm_filemap_label_page_cache WHERE ino = ?
        ),
        PageLifecycles AS (
            SELECT 
                timestamp as add_ts,
                (ofs / 4096) as page_idx,
                ofs as ofs_bytes,
                (SELECT MIN(timestamp) FROM mm_filemap_delete_from_page_cache d 
                 WHERE d.ino = a.ino AND d.ofs = a.ofs AND d.timestamp > a.timestamp) as del_ts
            FROM mm_filemap_add_to_page_cache a
            WHERE ino = ?
        )
        SELECT 
            add_ts, page_idx, ofs_bytes, del_ts,
            (SELECT COUNT(*) FROM AllAccess v 
             WHERE v.ofs_bytes = p.ofs_bytes 
             AND v.timestamp >= p.add_ts 
             AND (p.del_ts IS NULL OR v.timestamp <= p.del_ts)) as acc_count
        FROM PageLifecycles p
        '''
        df = pd.read_sql_query(query, conn, params=(curr_ino, curr_ino, curr_ino, curr_ino))

        if df.empty:
            continue

        max_ts = df[['add_ts', 'del_ts']].max().max()
        df['del_ts_fill'] = df['del_ts'].fillna(max_ts)
        df['duration'] = df['del_ts_fill'] - df['add_ts']
        df_zero = df[df['acc_count'] == 0].copy()

        if df_zero.empty:
            continue

        insert_batch = []
        for _, r in df_zero.iterrows():
            pg = int(r['page_idx'])
            ofs = int(r['ofs_bytes'])
            ofs_hex = f"0x{ofs:X}"
            add = r['add_ts']
            dur = r['duration']

            internal_file, comp, internal_ofs = "", "", ""
            if mapper:
                internal_file, comp, internal_ofs = mapper.trace(ofs)

            insert_batch.append((
                curr_ino, curr_filename, pg, ofs, ofs_hex, add, dur,
                internal_file, internal_ofs, comp
            ))

        if insert_batch:
            cur.executemany('''
                INSERT INTO zeroaccess_page (
                    ino, filename, page_idx, ofs_bytes, ofs_hex, add_ts, duration,
                    internal_file, internal_offset, compress_type
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ''', insert_batch)
            total_insert += len(insert_batch)
            print(f"✅ {curr_filename} | 零访问页：{len(insert_batch)} 个")

    conn.commit()
    print(f"\n🎉 分析完成！共写入 {total_insert} 条零访问页到 zeroaccess_page 表")
    conn.close()


# ==========================================
# 解析bitmap, 拆分出所有有效页偏移
# ==========================================
def parse_bitmap_to_offsets(base_ofs: int, bitmap_hex: str, page_size=4096):
    """
    :param base_ofs: o=后的起始偏移量
    :param bitmap_hex: 末尾16进制bitmap字符串
    :param page_size: 页大小默认4096
    :return: 每个有效页的真实偏移列表
    """
    # 16进制转换
    bitmap_val = int(bitmap_hex, 16)
    offset_list = []
    # 遍历0~63位，找出为1的bit位
    for bit_idx in range(64):
        if (bitmap_val >> bit_idx) & 1:
            real_ofs = base_ofs + bit_idx * page_size
            offset_list.append(real_ofs)
    return offset_list


# ==========================================
# 单个ftrace文件解析函数
# ==========================================
def parse_single_file(conn, ftrace_file):
    if not os.path.isfile(ftrace_file):
        print(f"error:无效文件: {ftrace_file}")
        return

    print(f"[{time.strftime('%H:%M:%S')}] 正在解析: {ftrace_file}")

    try:
        with open(ftrace_file, 'rb') as f:
            head = f.read(2)
        file_enc = 'utf-16' if head in (b'\xff\xfe', b'\xfe\xff') else 'utf-8'
    except:
        file_enc = 'utf-8'

    buffers = {
        'add': [], 'delete': [], 'fabit': [], 'access': [], 'label': [], 'bitmap': []
    }
    stats = {k: 0 for k in buffers}
    cursor = conn.cursor()
    BATCH_SIZE = 50000
    line_count = 0

    def flush_buffers():
        if buffers['add']: cursor.executemany('INSERT INTO mm_filemap_add_to_page_cache VALUES (?,?,?,?,?,?,?,?,?,?)', buffers['add'])
        if buffers['delete']: cursor.executemany('INSERT INTO mm_filemap_delete_from_page_cache VALUES (?,?,?,?,?,?,?,?,?,?)', buffers['delete'])
        if buffers['fabit']: cursor.executemany('INSERT INTO tracing_mark_fabit VALUES (?,?,?,?,?,?)', buffers['fabit'])
        if buffers['access']: cursor.executemany('INSERT INTO mm_filemap_access_history VALUES (?,?,?,?,?,?,?,?,?,?)', buffers['access'])
        if buffers['label']: cursor.executemany('INSERT INTO mm_filemap_label_page_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)', buffers['label'])
        if buffers['bitmap']: cursor.executemany('INSERT INTO bitmap_page_info VALUES (?,?,?,?,?,?,?)', buffers['bitmap'])
        conn.commit()
        for k in buffers: buffers[k].clear()

    try:
        with open(ftrace_file, 'r', encoding=file_enc, errors='ignore') as f:
            for line in f:
                line_count += 1
                line = line.strip('\x00\r\n\t ')
                if not line or line.startswith('#'):
                    continue

                # ========== 解析bitmap打点 ==========
                if 'tracing_mark_write' in line and 'bitmap' in line:
                    m = RE_BITMAP.search(line)
                    if m:
                        d = m.groupdict()
                        dev = d['dev']
                        # 10进制ino 转 0x开头16进制
                        ino_hex = f"0x{int(d['ino']):x}"
                        base_ofs = int(d['base_ofs'])
                        bitmap_hex = d['bitmap_hex']
                        ts = float(d['timestamp'])
                        pid = int(d['pid'])
                        pid_name = d['pid_name'].strip()

                        # 解析bitmap，拆分出所有真实页偏移
                        real_ofs_list = parse_bitmap_to_offsets(base_ofs, bitmap_hex)
                        for page_ofs in real_ofs_list:
                            buffers['bitmap'].append((
                                dev, ino_hex, base_ofs, page_ofs, ts, pid, pid_name
                            ))
                        stats['bitmap'] += len(real_ofs_list)

                elif 'mm_filemap_add_to_page_cache' in line:
                    m = RE_ADD.search(line)
                    if m:
                        d = m.groupdict()
                        mmapcnt = int(d["mmapcnt"]) if d["mmapcnt"] else 0
                        buffers['add'].append((d['dev'], d['ino'], d['page'], int(d['pfn']), int(d['ofs']), mmapcnt, d['flags'], float(d['timestamp']), int(d['pid']), d['pid_name'].strip()))
                        stats['add'] += 1

                elif 'mm_filemap_delete_from_page_cache' in line:
                    m = RE_DEL.search(line)
                    if m:
                        d = m.groupdict()
                        mmapcnt = int(d["mmapcnt"]) if d["mmapcnt"] else 0
                        # buffers['delete'].append((d['dev'], d['ino'], d['page'], int(d['pfn']), int(d['ofs']), int(d['mmapcnt']), d['flags'], float(d['timestamp']), int(d['pid']), d['pid_name'].strip()))
                        buffers['delete'].append((d['dev'], d['ino'], d['page'], int(d['pfn']), int(d['ofs']), mmapcnt, d['flags'], float(d['timestamp']), int(d['pid']), d['pid_name'].strip()))
                        stats['delete'] += 1

                elif 'tracing_mark_write' in line and 'fabit' in line:
                    m = RE_FABIT.search(line)
                    if m:
                        d = m.groupdict()
                        hex_ino = hex(int(d['ino']))
                        buffers['fabit'].append((d['dev'], hex_ino, int(d['ofs']), float(d['timestamp']), int(d['pid']), d['pid_name'].strip()))
                        stats['fabit'] += 1

                elif 'mm_filemap_mark_' in line:
                    m = RE_ACCESS.search(line)
                    if m:
                        d = m.groupdict()
                        buffers['access'].append((d['event_type'], d['dev'], d['ino'], d['page'], int(d['pfn']), int(d['ofs']), int(d['mmapcnt']), float(d['timestamp']), int(d['pid']), d['pid_name'].strip()))
                        stats['access'] += 1

                elif 'mm_filemap_label_page_cache' in line:
                    m = RE_LABEL.search(line)
                    if m:
                        d = m.groupdict()
                        buffers['label'].append((d['dev'], d['ino'], d['page'], int(d['pfn']), d['ofs'], int(d['mmapcnt']), int(d['label']), int(d['accessbit']), float(d['timestamp']), int(d['pid']), d['pid_name'].strip()))
                        stats['label'] += 1

                if line_count % BATCH_SIZE == 0:
                    flush_buffers()

        flush_buffers()
        print(f"解析完成: {os.path.basename(ftrace_file)} | add={stats['add']} del={stats['delete']} access={stats['access']}")
    except Exception as e:
        print(f"解析失败 {ftrace_file}: {str(e)}")


# ==========================================
# 批量处理文件夹所有文件
# ==========================================
def parse_all_files_in_folder(conn, folder_path):
    if not os.path.isdir(folder_path):
        print(f"error:文件夹不存在: {folder_path}")
        return

    all_files = [os.path.join(folder_path, f) for f in os.listdir(folder_path)
                  if os.path.isfile(os.path.join(folder_path, f))]

    if not all_files:
        print("warning:文件夹内无任何文件")
        return

    print(f"\n找到 {len(all_files)} 个文件，开始批量处理...\n")
    for idx, file in enumerate(all_files, 1):
        print(f"\n===== 处理第 {idx}/{len(all_files)} 个文件 =====")
        parse_single_file(conn, file)


# ==========================================
# 后置索引建立-可选操作
# ==========================================
# def build_indices(conn):
#     print(f"\n[{time.strftime('%H:%M:%S')}] 正在创建全库关联索引...")
#     cursor = conn.cursor()
#     cursor.execute('CREATE INDEX idx_add_pid_ino ON mm_filemap_add_to_page_cache(pid, ino, ofs)')
#     cursor.execute('CREATE INDEX idx_del_ino_ofs ON mm_filemap_delete_from_page_cache(ino, ofs)')
#     cursor.execute('CREATE INDEX idx_fabit_ino_ofs ON tracing_mark_fabit(ino, ofs, timestamp)')
#     cursor.execute('CREATE INDEX idx_access_ino_ofs ON mm_filemap_access_history(ino, ofs)')
#     cursor.execute('CREATE INDEX idx_label_ino_ofs ON mm_filemap_label_page_cache(ino, ofs)')
#     cursor.execute('CREATE INDEX idx_timestep_ts ON timestep(timestamp)')
#     cursor.execute('CREATE INDEX idx_smaps_ts ON process_smaps(timestamp)')
#     cursor.execute('CREATE INDEX idx_bitmap_dev_ino ON bitmap_page_info(dev, ino, page_ofs)')
#     conn.commit()
#     print("✅ 所有索引建立完毕！")


# ==========================================
# 主函数入口
# ==========================================
def main():
    # 依赖输入数据路径
    INODE_MAPPING_FILE = r"\inode.txt"
    FTRACE_INPUT_FOLDER = r"\hitrace"  #原始 ftrace 文件夹
    STEP_INFO_FILE = r'\PerformanceDynamicMeminfo_WechatKill_stepinfo.txt'
    SMAPS_ROOT_FOLDER = r"\smaps"   # 各进程 smaps 文件夹的根目录
    ARK_DISASM_FILE = r"\modules_disasm.txt"  # ark 反汇编文件
    HAP_FILE = r"\SceneBoard.hap"  # hap 文件路径

    # 分析结果保存路径路径
    DB_OUTPUT_PATH = "ftrace_ultimate_data_0715.db"      # 结果文件保存文件
    OUTPUT_DIR = Path("./resident_page_analysis_output")
    output_file = Path("./resident_page_analysis_output/zeroaccess_page_symbol_trace.csv")
    os.makedirs(output_file.parent, exist_ok=True)

    start_time = time.time()
    print("启动 Ftrace 批量分析...")

    db_conn = init_database(DB_OUTPUT_PATH)
    load_inode_mapping(db_conn, INODE_MAPPING_FILE)

    # # 解析用例步骤时间
    parse_step_info_to_db(db_conn, STEP_INFO_FILE)

    # 批量解析 smaps
    parse_smaps_folder_to_db(db_conn, SMAPS_ROOT_FOLDER)

    # # ark反汇编解析
    parse_ark_to_db(db_conn, ARK_DISASM_FILE)

    # 批量解析文件夹ftrace文件
    parse_all_files_in_folder(db_conn, FTRACE_INPUT_FOLDER)

    # build_indices(db_conn)
    db_conn.close()

    # 分析零访问页
    analyze_and_save_zero_access_pages(DB_OUTPUT_PATH, HAP_FILE)

    # 用 ADD 生命周期 [add_ts, del_ts] + 时间窗口判断零访问页
    analyze_and_save_zero_access_pages(
        db_path=DB_OUTPUT_PATH,
        hap_path=HAP_FILE,
        ino="0x42cc800",
        filename="/system/app/SceneBoard/SceneBoard.hap"
    )

    # 在 bitmap 中出现，且从未有过 fabit/access记录，就判定为冷页
    run_resident_page_analysis(DB_OUTPUT_PATH, OUTPUT_DIR, HAP_FILE)

    # 对零访问页进行函数溯源
    trace_zeroaccess_pages(DB_OUTPUT_PATH, output_file, "511:2", "0x42cc800")

    elapsed = time.time() - start_time
    print(f"\n🎉 全部文件处理完成！总耗时: {elapsed:.2f} 秒。")
    print(f"👉分析数据库已生成: {DB_OUTPUT_PATH}")


if __name__ == "__main__":
    # 执行文件页分析
    main()
