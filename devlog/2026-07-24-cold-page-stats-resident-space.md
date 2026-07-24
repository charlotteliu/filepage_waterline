# 2026-07-24 · cold_page_stats 增加平均驻留占用与冷页占比

在 `outputs/cold_page_stats.py`（[[2026-07-22 冷页统计]] 的后续）里增加两个统计量。

## 目标 / Plan

1. 平均文件页总体驻留占用多少 MB——不只是冷页的平均占用，而是**全部**闭合 add→delete 区间的平均驻留空间。
2. 冷页平均占用相对这个总体平均值的比值。

## 已实现 / Done

- [x] **平均驻留空间（`averageResidentSpace`）**：沿用冷页 spacetime 的算法，但把口径从"只算冷区间"放宽到"每一个闭合 add→delete 区间"，用完整的 `delete_ts − add_ts` 时长累加成 `residentPageSeconds`（page·秒），再除以测试窗口时长，得到 pages/bytes/MB 三种单位下的平均驻留页数——也就是整个测试期间任意时刻"平均驻留多少页/多少 MB"。
- [x] **冷页占比（`averageColdSpace.fractionOfAverageResident`）** = 平均冷页占用 ÷ 平均驻留占用，放在 `averageColdSpace` 字段里（跟它描述的对象放在一起，而不是单独摘出来），因为 pages/bytes/MB 三种单位算出来的比值完全一样（都是同一个 `PAGE_SIZE` 缩放），没必要重复三份。
- [x] `spacetime` 字段里同步加了 `residentPageSeconds`/`residentByteSeconds`/`residentPageMBSeconds`，跟已有的 `coldPageSeconds` 系列对称，方便对比。
- [x] 终端输出新增一行 `Avg resident space`，`Avg cold space` 那行末尾追加"占 avg resident space 的百分比"。

## 关键决定

- **"总体"的口径跟冷页一致**：只统计闭合的 add→delete 区间，窗口结束时仍驻留的页依旧不计入（跟原有的 `stillResidentAtEnd` 排除逻辑保持一致，不会出现"总体"和"冷页"两个统计口径不对齐的问题）。
- 复用同一个 `test_time`（观测窗口时长）做分母，保证"平均驻留空间"和"平均冷页空间"是在同一时间基准上算出来的，比值才有意义。

## 验证

手工构造 3 个页覆盖冷/热混合场景：
- 页 A（0 次访问，add@0 del@10）：冷，冷时长=10，总时长=10。
- 页 B（5 次访问，add@0 del@10）：热（不算冷），总时长=10（只贡献总体，不贡献冷页）。
- 页 C（1 次访问@3，add@0 del@10）：冷，冷时长=10−3=7，总时长=10。

预期：测试窗口=10s，总体 spacetime=30 → 平均驻留=3.0 页；冷 spacetime=17 → 平均冷页=1.7 页；比值=1.7/3.0=56.7%。实际输出与预期完全一致（`averageResidentSpace.pages=3.0`，`averageColdSpace.pages=1.7`，`fractionOfAverageResident=0.566667`）。

**真实文件验证**（`Camera_0030.db`）：平均驻留空间 23,932 页（93.486 MB），平均冷页占用 8,090 页（31.602 MB），冷页占平均驻留空间的 33.8%，8 秒跑完，数值量级合理（冷页体积应当明显小于总驻留体积，但不是可忽略的小头）。

## 后续可做

- 目前"总体"是全库口径的一个数；如果想看"哪些文件的冷页占比特别高"，可以在 `per_file` 里也顺带记一份该文件的总 spacetime，在 `topColdFiles`/CSV 里加一列该文件自己的 `fractionOfAverageResident`。
