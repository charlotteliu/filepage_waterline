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
