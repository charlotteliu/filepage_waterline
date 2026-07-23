# 2026-07-23 · 单进程独占、未映射页统计

新增可视化之外的数据统计功能：`outputs/single_process_page_stats.py`。

## 目标 / Plan

1. 统计测试 DB 里「所有加载（filemap add）以及 bitmap 出现过」的文件页，按 `(dev,ino,ofs)` 去重后的总量（"全体"）。
2. 在这个全体里，统计 `mmapcnt=0`（从未观测到有效 mmap 映射）且**只被单一一个进程访问过**的文件页总量，以及它占全体的百分比。

## 已实现 / Done

- [x] **全体（population）定义**：`mm_filemap_add_to_page_cache`（加载）∪ `bitmap_page_info`（bitmap 观测到）按 `(dev,ino,ofs)` 去重的并集。`delete`/`access` 表本身不产生新的全体成员，只用来补充已在全体里的页的 mmapcnt / 进程信息（否则窗口截断导致的孤立 delete/access 会污染"加载过的页"这个定义）。
- [x] **mmapcnt=0 判定**：取该页在 add/delete/access 三张表里所有行的 `mmapcnt` 最大值，等于 0 才算。`bitmap_page_info` 没有 `mmapcnt` 字段，如果一个页只在 bitmap 里出现过（从未见过 add/delete/access），视为"无 mmapcnt 数据"，**不计入** mmapcnt=0 的候选集——因为它出现在 bitmap 里这件事本身就说明它当时是被 mmap 映射的，跟"未映射"矛盾，默认成 0 会得出错误结论。
- [x] **"单一进程访问"判定**（已按用户纠正修改，见下方"追加修正"）：优先级级联而非并集——如果该页有 `mm_filemap_access_history` 记录，只用 access 的 `pid_name` 集合判定；否则退回用 add 的；如果 add 和 access 都没有（只有 delete），才用 delete 的。bitmap 快照自身的 `pid_name` 从不参与统计。
- [x] **性能**：不需要按时间戳做归并流式重建（不涉及驻留区间的状态机），只是对 4 张表各做一遍独立的全表扫描，按 `(dev,ino,ofs)` 聚合最大 mmapcnt + pid 集合即可；`--build-indices` 建 timestamp 索引仅用于加速 `--start`/`--end` 窗口过滤，不是必需。
- [x] `--json`/`--csv` 导出全部命中页（dev/ino/ofs/pageIdx/filename/maxMmapcnt/owningProcess/inAdd/inBitmap）；文件名解析复用了 `bitmap_filemap_diff.py` 里验证过的「只用 ino 匹配 inode_mapping，同一 ino 在结果里挂多个 dev 时视为歧义、留空」的逻辑（原地复制了一份，本仓库脚本一贯不共享模块，各自独立）。
- [x] 本开发日志 + 更新 `CLAUDE.md`（新增第 5 个工具条目）。

## 关键决定 / 假设

- **加载 = add 事件**，不包含 delete（"加载"字面意思就是被读入 page cache，对应 `mm_filemap_add_to_page_cache`）。
- **触碰=进程集合的口径**（已按用户纠正）：access > add > delete 优先级级联，不是并集；bitmap 自身 pid 永不参与。详见下方"追加修正"。
- delete/access 表里如果出现一个从未在 add 或 bitmap 里见过的 `(dev,ino,ofs)`（多半是窗口截断——页在窗口开始前就已加载，本次窗口只看到它被删除/访问），不计入全体，但计成 anomaly（`delete_without_add_or_bitmap` / `access_without_add_or_bitmap`），在 stderr/JSON 里报出来，不静默丢弃。

## 运行方式

```bash
python3 outputs/single_process_page_stats.py --db ftrace.db \
  --json single_owner_stats.json --csv single_owner_pages.csv --top 25
```

## 验证

用合成 DB 手工构造 6 个页覆盖全部边界情况，逐一核对：

| 页 | 场景 | 预期 |
|---|---|---|
| 0 | add mmapcnt=0，仅 procA | ✅ 命中 |
| 1 | add mmapcnt=0（procA）+ access（procB） | ❌ 两个进程 |
| 2 | add mmapcnt=3 | ❌ mmapcnt≠0 |
| 3 | 只在 bitmap 出现，从未 add/delete/access | ❌ 无 mmapcnt 数据（计入全体，不计入命中） |
| 4 | add mmapcnt=0（procE）+ bitmap 同一进程 procE | ✅ 命中（bitmap pid 与 add pid 相同，仍是单一进程） |
| 5 | add mmapcnt=0（procF）+ bitmap 不同进程 procG | ❌ 两个不同进程（add 侧 vs bitmap 侧） |

跑出来全体=6、命中=2（页 0、页 4），逐字段核对无误；`--no-bitmap-pid` 下页 5 变为命中（3/6=50%）、`--no-access` 下页 1 变为命中（3/6=50%），符合预期。

**真实文件验证**（`Camera_0030.db`）：
- 全体 432,466 页（add-only 403,804 / bitmap-only 27,895 / 两者都有 767，算术闭环：403804+27895+767=432466）。
- 单一进程 + mmapcnt=0 命中 209,344 页，占全体 48.41%。
- `noMmapcntData=19,779 < bitmap-only 27,895`：说明有 8,116 个"仅 bitmap 观测到"的页其实在 delete/access 表里也留了痕迹（大概率是窗口开始前就已加载、本窗口只看到它被删除/访问），因此仍然有 mmapcnt 数据，逻辑符合预期不是 bug。
- 耗时约 11 秒（DB 里 add/delete/access/bitmap 四表合计约 250 万行），可接受。
- 大量 anomaly（`delete_without_add_or_bitmap`=253,153，`access_without_add_or_bitmap`=157,618）——与 `CLAUDE.md` 里已经记录的"anomaly 计数非零通常代表窗口截断，不是代码 bug"结论一致（`Camera_0030.db` 本来就是截取的一段测试窗口）。

## 后续可做

- 目前只有全局统计 + 命中页明细，没有像 `bitmap_filemap_diff.py --all-bitmap-files` 那样按文件聚合"这个文件里有百分之多少的页是单一进程独占未映射"；如果这是常见需求，可以加一个按 `(dev,ino)` 聚合的第二张报表。

## 追加修正：进程口径纠正

用户指出初版的"触碰进程"定义有两处错误：

1. **bitmap 快照的 `pid_name` 不是真正的访问者**——是抓取 bitmap 时跑的 `cat` 之类 shell 命令的 PID，跟这个页真正被谁访问毫无关系，不应该参与统计。初版把它并入进程集合是错的，直接删掉了这部分逻辑（连同 `--no-bitmap-pid` 开关一起移除，因为这个数据源本来就不该有开关，直接不用）。
2. **"访问"应该是优先级级联，不是几个来源的并集**：有 access_history 记录就只看 access_history 的 `pid_name`（这才是真正意义上的"谁访问了这个页"）；没有 access_history 才退而求其次看 add 的 `pid_name`；add 和 access 都没有（只剩 delete）才用 delete 的。初版把 add/delete/access 全部揉在一个集合里求并集，会把"A 进程加载、B 进程访问"误判成"两个进程"——但如果有访问记录，加载者是谁并不重要，只有真正访问过的进程才算"owner"。

### 修复

- `outputs/single_process_page_stats.py` 里每个页的状态从一个共享 `pid_names` 集合拆成三个独立集合：`add_pids` / `delete_pids` / `access_pids`；bitmap 扫描不再读取、也不再写入任何 pid 信息。
- 新增 `owning_pids(st)`：`access_pids` 非空就返回它；否则 `add_pids` 非空就返回它；否则返回 `delete_pids`（哪怕是空集）。"单一进程"判定改成 `len(owning_pids(st)) == 1`。
- 命中页新增 `ownerSource` 字段（`access`/`add`/`delete`），标明这个页的 owner 是从哪个来源判定的，JSON/CSV/控制台输出都带上，方便核对。
- `--no-access` 语义不变：跳过扫描 access_history 表，级联自然退到 add→delete。

### 验证

构造 5 个页覆盖级联的每一种落点，逐一核对：

| 页 | 场景 | 预期 owner 来源 | 预期结果 |
|---|---|---|---|
| 0 | access 两个进程（X,Y）+ add 一个进程（A） | access | ❌ 不命中（access 侧 2 个进程） |
| 1 | access 一个进程（Z）+ add 另一个不同进程（B） | access | ✅ 命中，owner=Z（add 的 B 被忽略） |
| 2 | 无 access，add 一个进程（C）+ delete 另一个不同进程（D） | add | ✅ 命中，owner=C（delete 的 D 被忽略） |
| 3 | 无 access 无 add，只有 delete（E）+ bitmap（进程"cat"） | delete | ✅ 命中，owner=E（bitmap 的 "cat" 被忽略） |
| 4 | add 一个进程（F）+ bitmap 不同进程"cat" | add | ✅ 命中，owner=F（bitmap 的 "cat" 被忽略） |

实际输出：全体=5，命中=4（页 1/2/3/4），`ownerSource` 分别是 access/add/delete/add，与预期完全一致；页 0 正确排除。`--no-access` 下页 0、页 1 都改用 add 的 pid（procA/procB）命中，全体=5、命中=5，符合级联退化预期。

**真实文件重新验证**（`Camera_0030.db`）：全体不变（432,466，因为全体定义没有改动），命中数从旧口径的 209,344（48.41%）变为新口径的 251,432（58.14%）——口径变宽是预期的，因为新逻辑不再要求"add 和 access 都必须只有一个进程"，只看优先级最高的那个来源即可。

## 追加修正：内存占用治理

用户反馈：目标 DB 30G 的情况下，脚本执行占用了超过 20G 内存。

### 根因

聚合表 `agg: Dict[(dev,ino,ofs), list]`，每个页的状态是 `[in_add, in_bitmap, max_mmapcnt, has_mmapcnt, add_pids:set, delete_pids:set, access_pids:set]`。CPython 里一个空 `set()` 本身就有约 216 字节的固定开销，一个页 3 个 set（哪怕大多数时候只装 0~1 个 pid_name）就是 650+ 字节，再加上 `(dev, ino, ofs)` 三元组本身作为 dict key 的容器开销（约 72 字节）——**同一个文件的成千上万个页，每一个都各自重复存一份 `dev`/`ino` 的引用和一个全新的 tuple 容器**，而不是像 `cold_page_stats.py` 那样把 `dev|ino` 当成文件级别的 key 只存一份。实测（见下方 benchmark）这套结构在真实进程名/设备号的重复度下高达 **897.9 字节/页**；如果 30G 的 DB 对应两三千万到数千万级别的去重页数，20G+ 内存完全对得上。

另外，`analyze()` 会把全体 population 先物化成一个 list（`population = [k for k, st in agg.items() if ...]`），再遍历它去建 `qualifying` list——相当于在已经很大的 `agg` 之外，又叠了一层与 population 同量级的中间列表；而且不管有没有 `--json`/`--csv`，命中的每一页都会被塞进 `qualifying` list，哪怕用户只要终端上打印的 `--top 25`。`agg` 本身也会一直存活到 `main()` 结束（打印、导出全程都在），没有在统计完之后尽早释放。

### 修复

1. **聚合表改成两层嵌套**：`agg: Dict[str, Dict[int, list]]`，外层 key 是 `intern("dev|ino")`（复用 `cold_page_stats.py` 里 `make_file_key` 的约定），内层才是 `ofs -> state`。同一个文件的所有页不再各自持有一份 `dev`/`ino` 字符串引用和一个三元组容器，只在外层字典里存一次。
2. **每页状态从 7 个字段（3 个 set）压缩成 5 个字段、0 个 set**：`[flags:int, max_mmapcnt:int, add_pid, delete_pid, access_pid]`。`in_add`/`in_bitmap`/`has_mmapcnt` 和"这个来源见过 >1 个不同 pid"三个标志位全部压进一个 int 位掩码；`add_pid`/`delete_pid`/`access_pid` 只存**第一次见到的 pid_name**（或 `None`），第二次出现不同名字时只翻转对应的 multi 位——因为我们自始至终只需要回答"这个来源是不是恰好一个不同的进程"，不需要真的知道有几个、都叫什么。
3. **`sys.intern()` 处理 `dev`/`ino`/`pid_name`**：这三类字符串在真实数据里重复度极高（设备号几个、进程名几百个），sqlite3 每次取行都会分配新的 str 对象，`intern()` 把它们收敛成同一个对象，副作用是相同字符串的 dict 查找也会变快（CPython 对 interned 字符串做指针相等的快速路径）。
4. **`analyze()` 不再预物化 population 列表**，直接嵌套双重循环边扫边统计；`qualifying` 改成"数量始终精确统计，但只在需要完整导出（`--json`/`--csv`）时才保留全部命中行，否则只保留 `--top` 需要的那几条"——终端汇总模式下，不管命中了多少页，内存占用都不会随命中数增长。
5. **`main()` 里 `analyze()` 一结束就 `del agg`**：报表阶段（求 owner pid、算 pageIdx、解析 filename、打印、写 JSON/CSV）此后都不再依赖那个可能几十 GB 的聚合表，尽早交还给 GC，而不是拖到函数结束。

### Benchmark（tracemalloc，200 万模拟页 / 2000 个文件 / 6 个 dev / 200 个进程名）

| 实现 | 峰值内存 | 字节/页 |
|---|---|---|
| 旧（set×3 + 扁平三元组 key） | 1795.8 MB | 897.9 |
| 新（flags+可空pid + 嵌套字典 + intern） | 346.2 MB | 173.1 |

**约 5.19x 缩减**。按报告的场景估算，20G 实测内存 × (173.1/897.9) ≈ **3.9G**，量级上应该能把 30G DB 的运行控制在多数机器可承受的范围内。

### 验证

- 用之前"优先级级联"验证用的合成 DB（5 个页覆盖 access/add/delete 三种 owner 来源 + `--no-access` 退化）重新跑：**输出与重构前逐字节一致**（全体=5，命中=4，`ownerSource` 分布不变；`--no-access` 下全体=5命中=5）。
- 新增验证 `--top` 与导出的解耦：`--top 2`（不带 `--json`/`--csv`）时终端只打印 2 条，但汇总的"命中数"仍然是完整的 4；同样 `--top 2 --json` 时终端仍只打印 2 条，但写出的 JSON `qualifyingPages` 完整包含全部 4 条、`pageCount` 也是 4——确认"只裁剪展示、不裁剪统计口径"符合预期。
- 真实库 `Camera_0030.db` 重新跑：**结果与重构前完全一致**（全体 432,466、命中 251,432、58.14%、anomaly 计数不变），且耗时从约 12.3 秒降到约 7.85 秒（去掉了创建/销毁上百万个 `set()` 对象的开销，GC 压力更小，反而更快，符合"控制内存的前提下不能牺牲性能"的要求）。

## 追加：mmapcnt 逻辑复核（无需改动）

用户怀疑 mmapcnt 统计只用了"某一次"的值，要求确认是否已经按 `max_mmapcnt == 0` 选取。复核 `collect()`：`ADD_TABLE`/`DELETE_TABLE`/`ACCESS_TABLE` 三处都对**同一个共享的 per-page state**执行 `if mmapcnt > st[S_MMAPCNT]: st[S_MMAPCNT] = mmapcnt`，因为三处都是通过同一个 `get(file_key, ofs)` 拿到同一个列表对象，所以本来就是在整段扫描范围内取所有 add/delete/access 事件的最大值，不是"某一次"。

用构造数据实测验证（而不是只看代码）：
- 页 0：access 历史依次是 `0,0,5,0`（**最后**一次是 0）——如果 bug 是"只用最后一次的值"，会误判为命中；实际正确判定为真实最大值 5，不命中。
- 页 1：`add=3`（**第一次**）之后 access/delete 都是 0——如果 bug 是"后来的小值覆盖了之前的大值"，会误判为命中；实际正确保留最大值 3，不命中。
- 页 2（对照组）：全程都是 0 → 正确命中。

跑出来全体=3、命中=1（只有页 2），跟预期完全一致，确认现有实现没有问题，未做代码改动。

## 追加：--db-table 参数

用户要求增加把结果落到 DB 内一张表的能力（此前只有 `--json`/`--csv`）。

- 新增 `--db-table NAME`：把命中页列表（跟 `--csv` 同一份数据，多了 `dev`/`ino` 两列方便脱离命令行上下文单独查询）写入 `--db` 指向的**同一个源库**里的一张表，`DROP TABLE IF EXISTS` + `CREATE` + 批量 `INSERT`（覆盖式刷新，不是追加）。用独立的可写连接（复用已有的 `connect(db_path, writable=True)`），不影响主流程的只读连接。
- `--db-table` 和 `--json`/`--csv` 一样会触发"需要完整导出"（`need_full_export`），跳过 `--top` 截断,保证写入的表是完整命中集合，不是被 `--top` 裁剪过的子集。
- 验证：合成 DB 上两次运行确认第二次是覆盖（1 行还是 1 行，不是 2 行）；真实库 `Camera_0030.db` 上完整跑了一次，写入 `single_owner_unmapped_pages` 表 251,432 行，与终端汇总的命中数一致，7 秒完成。**注意：这次验证顺带往用户提供的真实文件 `Camera_0030.db` 里新增了这张表**（符合本次需求就是要往源库写表，非意外副作用，告知用户留档）。
