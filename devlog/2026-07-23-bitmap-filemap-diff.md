# 2026-07-23 · bitmap vs filemap 页差异对比

新增可视化之外的数据对比工具：`outputs/bitmap_filemap_diff.py`。

## 目标 / Plan

同一个文件的页可以从两个独立来源出现在 DB 里：

- `bitmap_page_info`：`tracing_mark_write ... bitmap` 周期性打点，反映**某一个被追踪进程**地址空间里当前 mmap 映射的页（64bit bitmap 按位拆成逐页记录，见 `parse_bitmap_to_offsets`）。
- `mm_filemap_add_to_page_cache` / `mm_filemap_delete_from_page_cache`：**整机**的 page-cache 生命周期记录（mmap、read()、readahead 等所有路径都会产生）。

初版需求：给定一个文件（inode），找出「有 filemap add/delete 记录，但 bitmap 从未见过」的页。

**中途澄清（关键假设修正）**：bitmap 的采集范围是单进程地址空间的 mmap 映射，而 filemap 是整机记录，两者范围本就不对等——很多文件只会出现在 filemap（该进程从未 mmap 过）而不出现在 bitmap，这种差异是正常现象、不值得关注。真正有意义的对比应该**限定在 bitmap 实际观测到的文件集合内**，看这些文件的 bitmap 页和 filemap 页对不对得上。因此追加了批量模式，对 bitmap 见过的所有文件做交集对比统计，而不是只人工指定一个文件。

## 已实现 / Done

- [x] **单文件模式**（`--ino`/`--dev` 或 `--file-like` 定位文件）：对比该 `(dev,ino)` 的 bitmap 页集合（`page_ofs` 去重）与 filemap add/delete 页集合（`ofs` 去重），列出 filemap 有但 bitmap 没有的页，每页附带 add/del 次数、首末时间戳、是否仍驻留、最大 mmapcnt、最后归属 pid_name。同时报告 both / bitmap-only 计数供参考。
- [x] **批量模式**（`--all-bitmap-files`）：枚举 `bitmap_page_info` 出现过的全部 `(dev,ino)`，只在这个范围内对比 filemap，输出每个文件的 bitmapPages / filemapPages / bothPages / filemapOnlyPages / bitmapOnlyPages 及占比，按 `--rank-by`（默认 `filemap_only`）排序，方便直接定位"bitmap 观测到但 filemap 记录缺口最大"的文件。
- [x] **性能**：批量模式不是对每个文件单独发 SQL（423 个文件 × 2 张大表会是灾难），而是复用可视化器 `--file-like` 已经验证过的模式——先把 bitmap 侧的 `(dev,ino)` 收集成一个 Python 集合，再对 add/delete 表各做一次流式全表扫描、用集合成员判断过滤，固定两次全表扫描，与文件数无关。单文件模式仍是按 `dev,ino` 的点查询，可选 `--build-indices` 建 `(dev,ino)` 索引加速重复调用。
- [x] `--json`/`--csv` 导出（单文件模式导出逐页明细；批量模式导出逐文件汇总）。
- [x] 本开发日志 + 更新 `CLAUDE.md`（新增第 4 个工具条目、`bitmap_page_info` 表结构说明）。

## 关键决定 / 假设

- **页粒度对齐**：`bitmap_page_info.page_ofs` 和 filemap 表的 `ofs` 都是字节偏移（非 page index），可直接集合比较；这与 `tracing_mark_fabit` 表的 `ofs`（page index，需要 ×4096）不同，混用会得出错误结论，脚本没有涉及 fabit 表。
- **ino 归一化**：`--ino` 接受十进制或 `0x` 十六进制，统一转成 `f"0x{val:x}"` 去匹配 add/delete/bitmap 三张表里已经是十六进制字符串的 `ino` 列（与 `load_ftrace_to_db_file.py` 里的转换方式一致）。
- **批量模式的"交集"定义**：只要该文件在窗口内出现过至少一条 bitmap 记录就纳入报表；`bothPages=0` 的文件（bitmap 看到但该窗口内完全没有 filemap add/delete 记录）也会出现在报表里，代表窗口开始前就已经驻留、或者 mmap 短暂存在未触发新的 add/delete。
- 该功能没有依赖 `inode_mapping`；如果 DB 没有这张表（真实验证用的 `Camera_0030.db` 就没有），报表 filename 列留空，仍可用 `dev/ino` 定位。

## 运行方式

```bash
# 单文件：找出某个文件里 filemap 有、bitmap 没见过的页
python3 outputs/bitmap_filemap_diff.py --db ftrace.db --ino 0x1234 --dev 8:1

# 按文件名模糊定位（需要 inode_mapping 表）
python3 outputs/bitmap_filemap_diff.py --db ftrace.db --file-like '%libfoo.so%'

# 批量：bitmap 观测到的所有文件做交集对比报告
python3 outputs/bitmap_filemap_diff.py --db ftrace.db --all-bitmap-files \
  --rank-by filemap_only --json bitmap_diff.json --csv bitmap_diff.csv
```

## 验证

- 用合成 DB（手工构造 5 个 add / 3 个 delete / 3 条 bitmap 记录）验证单文件模式：filemap 5 页、bitmap 3 页（2 个重合 + 1 个 bitmap 独有），正确识别出 3 个 filemap-only 页，字段（add/del 次数、首末时间戳、是否仍驻留）逐一核对无误。
- 追加一个"纯 mmap、无 filemap 记录"的合成文件验证批量模式边界情况：`filemapPages=0, bothPages=0, bitmapOnly=4`，排序及汇总计数正确。
- `--all-bitmap-files` 与 `--ino`/`--file-like` 同传会报错拒绝。
- **真实文件验证**（`Camera_0030.db`，~390MB，无 `inode_mapping` 表）：
  - 单文件模式：随机挑了 3 类样本（bitmap/filemap 有交集、bitmap 完全没覆盖到的高频文件、省略 `--dev` 靠自动推断）跑通，均在 1 秒内完成（无索引也够快，因为是单表全扫）。
  - 批量模式：bitmap 共观测到 423 个 `(dev,ino)` 文件，164 个与 filemap 有交集、259 个交集为 0；交集内 filemap-only 1,616 页、bitmap-only 27,861 页；1.2 秒跑完。JSON/CSV 导出内容用 `both+filemapOnly=filemapPages`、`both+bitmapOnly=bitmapPages` 做了算术闭环校验，全部一致。

## 后续可做

- 单文件模式当前 `--ino`/`--dev` 需要用户手动定位；如果常用场景是"给定文件名找 bitmap 缺口"，可以让批量模式也支持 `--file-like` 作为**过滤器**（而不是唯一定位），先圈定候选集合再跑批量对比。
- 批量模式目前只统计计数，没有像单文件模式一样导出逐页明细；如果需要对 Top N 文件做页级下钻，可以加一个 `--detail-top N` 复用 `build_missing_records` 逐个文件补全。
- 复用 `--start`/`--end` 时间窗口时，`bothPages=0` 的"假交集缺失"文件可能只是窗口切得不巧（该文件驻留区间横跨窗口边界），后续可以考虑把窗口外但邻近的 add/delete 事件也纳入判断。

## 追加：Issue #1「filemap bitmap diff加强」

GitHub issue [#1](https://github.com/charlotteliu/filepage_waterline/issues/1) 用真实大库（`ftrace_ultimate_data_0602.db`，`--all-bitmap-files` 输出 5,567 个 bitmap 文件 / 149 万 filemap 页）提了 3 个具体缺陷，本次逐一修复：

### 1. inode_mapping 按 (dev,ino) 精确匹配导致文件名丢失

**问题**：`inode_mapping.dev` 和 ftrace 事件表上报的 dev 不一定一致（采集路径不同），原实现要求 `dev=? AND ino=?` 精确匹配，dev 一旦对不上就查不到文件名。

**修复**：新增 `resolve_filenames_by_ino(conn, keys)`，只用 `ino` 去 `inode_mapping` 查（`ino` 本来就是该表主键，天然唯一）。但报告自身范围内（例如批量模式收集到的全部 `(dev,ino)`）如果同一个 `ino` 挂在**两个不同的 dev** 下（不同文件系统/挂载点复用了相同 inode 号，或者采集路径不一致导致同一个文件被记成了两个 dev），就是真正的歧义——不知道这条 `inode_mapping` 记录到底描述哪一个，两边都不填文件名，只显示 `dev:ino`。单文件模式只有一个 key，天然不会有这种歧义，因此只要 ino 命中就一定能拿到文件名（即使 dev 对不上）。

用合成数据验证：ino 命中但 inode_mapping 里 dev 是错的（且我们自己范围内该 ino 只对应一个 dev）→ 正确拿到文件名；同一个 ino 在批量结果里挂了两个不同 dev → 两行文件名都正确留空。

### 2. 缺少 add/access 进程追踪

**问题**：只知道页被谁 add/delete（`lastPidName`，还只是最后一次），看不出这个文件到底被哪些进程写入、被哪些进程访问过。

**修复**：
- 单文件模式：`load_filemap_aggregate` 新增按 ofs 记录 `addProcesses`（add 事件的 pid_name 集合），新增 `annotate_access_processes()` 读 `mm_filemap_access_history` 补上每个 ofs 的 `accessProcesses`。
- 批量模式：`load_filemap_offsets_for_keys` 顺带收集每个文件的 add 进程集合；新增 `load_access_procs_for_keys()` 对 `mm_filemap_access_history` 做同样的单遍全表扫描 + key-set 过滤，收集每个文件的 access 进程集合。
- 两处都作为新增列（JSON 列表 / CSV 分号分隔字符串）输出，不影响原有字段。
- `mm_filemap_access_history` 是可选表（不存在时该功能静默跳过，不报错）。

真实库 `mm_filemap_access_history` 有 152 万行，批量模式加上这一遍扫描后总耗时从 1.2s 涨到 3.3s（423 个文件），仍然可接受；用合成数据验证了单页多进程 add / 多进程 access 的场景，列表内容和排序（`sorted()`）符合预期。

### 3. 导出只能写 CSV，一次只有一份

**问题**："完整输出信息写 CSV，需要能按需要写到不同的表"——原来只有 `--csv`，每次固定文件、固定单一结构，不方便把多次跑的结果落到同一个库里分表管理、后续用 SQL 查询/关联。

**修复**：新增 `--sqlite-out PATH [--sqlite-table NAME]`，把报告写入（DROP+CREATE，覆盖式刷新而非追加）一个独立可指定名字的 SQLite 表：
- 单文件模式默认表名 `bitmap_filemap_diff_single`，列在原 CSV 基础上补了 `dev`/`ino`（CSV 本身省略是因为单次运行只对应一个文件，SQLite 表要独立可查询所以补全）。
- 批量模式默认表名 `bitmap_filemap_diff_batch`。
- 用 `--sqlite-table` 可以按次/按库自定义表名，同一个输出库里可以并存多次分析结果。

用合成数据验证了两种模式写表都成功，字段类型和内容通过独立连接读回核对一致。

### 验证汇总

- 合成数据覆盖了三个缺陷各自的边界场景（dev 错配但唯一可解析 / 同 ino 跨两个 dev 的真歧义 / 多进程 add 与 access / 两种模式的 sqlite 落表）。
- 真实库 `Camera_0030.db` 重新跑单文件与批量模式：**总量结果与加固前完全一致**（423 个 bitmap 文件、164 个有交集、filemap-only 1,616、bitmap-only 27,861），确认三处改动没有影响原有对比逻辑，只是新增字段/新增文件名解析路径。
- 批量模式性能：3.3 秒（含新增的 access_history 152 万行扫描），可接受。

### 未做 / 留给用户确认

- 无法用 `gh` CLI 或直接网络访问关闭该 issue（本环境沙箱无出网权限，`gh` 也未安装），已用 `WebFetch` 读取 issue 内容用于确认需求，但没有写权限去评论/关闭；需要用户自行在 GitHub 上处理。
