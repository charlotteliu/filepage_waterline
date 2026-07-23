-- 文件页的逻辑主键使用 (dev, ino, ofs)。
-- 不使用 page/pfn：页被回收并重新装入后，物理页地址和 PFN 可能变化。
--
-- “单一进程访问”定义为整个采样期内 COUNT(DISTINCT pid) = 1。
-- “mmapcnt = 0”采用严格口径：该页的所有访问记录均为 0。
-- 因为 mmapcnt 非负，所以 MAX(mmapcnt) = 0 等价于始终为 0。

WITH pages AS (
    SELECT
        dev,
        ino,
        ofs,
        COUNT(*) AS access_events,
        COUNT(DISTINCT pid) AS pid_count,
        MIN(mmapcnt) AS min_mmapcnt,
        MAX(mmapcnt) AS max_mmapcnt
    FROM mm_filemap_access_history
    GROUP BY dev, ino, ofs
)
SELECT
    COUNT(*) AS total_accessed_file_pages,
    SUM(pid_count = 1 AND max_mmapcnt = 0) AS target_file_pages,
    ROUND(
        100.0 * SUM(pid_count = 1 AND max_mmapcnt = 0) / COUNT(*),
        6
    ) AS target_percentage
FROM pages;

-- 完整目标页清单。
SELECT
    a.dev,
    a.ino,
    a.ofs,
    MIN(a.pid) AS pid,
    GROUP_CONCAT(DISTINCT a.pid_name) AS pid_names,
    0 AS mmapcnt,
    COUNT(*) AS access_events,
    COUNT(DISTINCT a.event_type) AS event_type_count,
    GROUP_CONCAT(DISTINCT a.event_type) AS event_types,
    COUNT(DISTINCT a.page) AS cache_page_instances,
    COUNT(DISTINCT a.pfn) AS pfn_instances,
    MIN(a.timestamp) AS first_timestamp,
    MAX(a.timestamp) AS last_timestamp,
    m.filename,
    m.size AS file_size
FROM mm_filemap_access_history AS a
LEFT JOIN inode_mapping AS m
    ON m.ino = a.ino
GROUP BY a.dev, a.ino, a.ofs
HAVING COUNT(DISTINCT a.pid) = 1
   AND MAX(a.mmapcnt) = 0
ORDER BY
    m.filename,
    a.dev,
    a.ino,
    a.ofs;
