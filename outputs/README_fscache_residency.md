# 文件页缓存驻留生命周期可视化

这个小工具根据 SQLite DB 中的 `add/access/delete` 事件重建文件页缓存驻留状态，并生成一个可以直接打开的独立 HTML。

## 输入假设

- 文件页身份：`(dev, ino, ofs)`
- `mm_filemap_add_to_page_cache`：页面进入 page cache，开始 resident interval
- `mm_filemap_delete_from_page_cache`：页面离开 page cache，结束 resident interval
- `mm_filemap_access_history`：页面被访问，不改变 resident 状态，但会把该 resident 页的当前归属 `pid_name` 更新为访问者，用于第二种“按 pid_name 着色”
- `inode_mapping`：把 `(dev, ino)` 映射到文件名
- `timestep`：作为时间轴上的业务步骤参考线

## 推荐用法

```bash
python3 /Users/gavinliu/Documents/Codex/2026-06-30/db-users-gavinliu-downloads-fscachedb-md/outputs/fscache_residency.py \
  --db /path/to/ftrace_ultimate_CHZ_1h.db \
  --html /Users/gavinliu/Documents/Codex/2026-06-30/db-users-gavinliu-downloads-fscachedb-md/outputs/fscache_residency.html \
  --json /Users/gavinliu/Documents/Codex/2026-06-30/db-users-gavinliu-downloads-fscachedb-md/outputs/fscache_residency.json
```

生成后打开 `fscache_residency.html` 即可。页面上方是整机 resident page 数量随时间变化；下方是抽样文件页的生命周期条带。左上按钮可在“按文件着色”和“按 pid_name 着色”之间切换。

## 大库建议

完整库有上亿事件时，首次生成可能需要较久。可以先用时间窗口或过滤条件验证：

```bash
python3 /Users/gavinliu/Documents/Codex/2026-06-30/db-users-gavinliu-downloads-fscachedb-md/outputs/fscache_residency.py \
  --db /path/to/ftrace_ultimate_CHZ_1h.db \
  --start 80 --end 140 \
  --file-like '%/data/%' \
  --max-lanes 400 \
  --html /Users/gavinliu/Documents/Codex/2026-06-30/db-users-gavinliu-downloads-fscachedb-md/outputs/fscache_residency_80_140.html
```

常用参数：

- `--start / --end`：限制 timestamp 区间
- `--file-like`：按 `inode_mapping.filename` 做 SQL LIKE 过滤
- `--pid-like`：按 `pid_name` 做 SQL LIKE 过滤
- `--bucket-seconds`：聚合曲线的桶宽
- `--max-groups`：聚合图保留多少个 top 文件和 top pid_name
- `--max-lanes`：生命周期图保留多少个文件页条带
- `--no-access`：只用 add/delete 重建驻留，不用 access 更新 pid_name

## 输出解释

- 聚合图：每个 bucket 记录当前 resident pages 总数，并按文件或 pid_name 分组堆叠。
- 生命周期图：每一行是一个文件页；彩色条表示该页在 page cache 中 resident；竖线表示 add/access/delete 事件。
- 异常计数：脚本会记录 `delete_without_active_add`、`access_without_active_add`、`duplicate_add_closed_previous`，用于判断 trace 是否有窗口截断或事件丢失。
