# 2026-07-22 · 冷页统计（cold-page statistics）

新增可视化之外的数据统计功能：`outputs/cold_page_stats.py`。

## 目标 / Plan

1. **找出冷页**：测试期间，`add` 事件到 `delete` 事件之间访问次数为 0 或 1 的文件页。
   文件页由 `(dev, ino, ofs)` 唯一确定。访问事件包括：
   - `mm_filemap_access_history` 中的 `mark_access` / `mark_reaccess` / `mark_referenced`（该表每一行都是访问）；
   - `mm_filemap_label_page_cache` 中 `label > 1` 的行（“access 大于 1 也作为访问事件”）。
2. **统计冷页 spacetime**（space×time，单位 page·秒）：
   - 0 次访问：spacetime = `delete_ts − add_ts`；
   - 1 次访问：spacetime = `delete_ts − access_ts`（唯一一次访问之后到删除的时长）。
3. **平均冷页占用 space** = 冷页整体 spacetime / 测试时间，即整个测试窗口内平均驻留的冷页数量（页 / 字节 / MB）。
4. 将本次工作的目标、plan、以及实现情况记录到开发日志目录（本文件）。

## 已实现 / Done

- [x] **冷页识别**：按时间戳流式重建每个 `(dev,ino,ofs)` 的 add→delete 驻留区间，统计区间内访问次数；`≤1` 判为冷页，并区分 0 次 / 1 次。仅统计闭合的 add→delete 区间（必须有 delete）；测试结束仍驻留的页单独报告、不计入。
- [x] **访问事件来源**：access_history 全部行 + label 表 `label > 1` 的行；`--no-label-access` 可关闭 label 来源，`--label-access-gt N` 可调阈值。
- [x] **spacetime 统计**：按上述规则累加，输出 page·秒、byte·秒、MB·秒。
- [x] **平均冷页占用 space** = 冷页 spacetime / 测试时间，输出 pages / bytes / MB。
- [x] **按文件聚合**：`--top N` 列出 spacetime 最高的冷页文件（含 filename）；`--csv` 导出全部文件级冷页统计。
- [x] **性能**：与可视化器一致的流式方案——各源表按 timestamp 索引游标 + `heapq.merge`（无排序、内存有界）；缺索引时回退到 `UNION ALL ... ORDER BY`（临时 B 树排序）。`--build-indices` 一次性建好 add/delete/access/label 的 timestamp 索引；`temp_store=FILE` + 大 cache + mmap 避免大库 OOM。
- [x] 本开发日志。

## 关键决定 / 假设

- **“access 大于 1” 指 label 表的 `label` 列**：label 表有 `label` 与 `accessbit` 两列；`accessbit` 是 0/1 位（无法 `>1`），因此 `label > 1` 才是题意。demo 库中所有 label 行 `label=2, accessbit=1`，与该判断一致。阈值可用 `--label-access-gt` 调整；若应为其它列，改 `_stream` / 查询中的 `label` 即可。
- label 事件与 access_history 事件**累加计数**（“也作为访问事件”），未做去重。
- 每个 add→delete 区间独立判定；同一页多次 add→delete 各自评估。重复 add（无 delete）丢弃前一个开区间并计入 anomaly。

## 运行方式

```bash
# 一次性建索引（大库强烈建议），然后统计
python3 outputs/cold_page_stats.py --db /path/to.db --build-indices \
  --json cold_stats.json --csv cold_by_file.csv --top 25
```

## 验证（demo 库 top_apps_fscache_demo.db）

- 事件计数自洽：`add(45100)+del(36322)+access(99503)+label>1(8778)=189703` = 处理事件总数；`add−del=8778` = 结束仍驻留数。
- **快/慢两条路径结果完全一致**（heapq.merge 索引流 vs UNION 排序）：intervals / spacetime / averageColdSpace 全部相等。
- 结果：闭合区间 36,322；冷页 12,675（0 次 3,625 / 1 次 9,050，占 34.9%）；冷页 spacetime 710,856 page·s；平均冷页占用 238.0 页（0.930 MB）。
- Top 冷页文件为各 app 的 `background/*.bin`，与冷热热力图结论一致。

## 后续可做

- 支持把测试结束仍驻留的页按 `end_ts` 视作 delete 计入（开关）。
- 复用 `--file-like` / `--exclude-file` 过滤。
- 可选导出逐页（dev,ino,ofs 级）明细 CSV（大库慎用）。
