# eval100-code-v7 分析报告

* Job: `jobs/eval100-code-v7/`（100/100 完成，均值 **0.41**：41×1.0，59×0.0）
* 对比基线：`jobs/eval100-code-v6-1/`（43×1.0 / 56×0.0 / 3 error，均值约 0.43；`eval100-code-v6` 仅 19 完成，不做对比）
* Harness 差异只有一行：`harnesses/code-v7.yaml = code-v6 + tools.enabled+=write_file`
  （`[run_command, read_file, apply_patch, edit_file]` → 再加 `write_file`）
* Tool prefer 定义：`src/agent_runtime/tools/tools.py:77-336`
* 计数口径：`tool.start` 为准（`llm.end.messages` 经 `llm_summary` 压缩只剩约 70% 调用，只做意图抽样）

## 1. 59 个 0 分 task

按 repo：matplotlib 8/8、mwaskom 2/2、pylint 8/9、django 7/10、sympy 7/10、
sphinx 6/10、pytest 6/11、astropy 5/10、pydata 4/11、sklearn 4/11、psf 2/7、pallets 0/1。

两大类：

**A. 从未落盘（22/59，一次 edit/apply 都没有）：**
astropy-13398/13977/14369，django-14725/15128，matplotlib-25775/26208，
seaborn-3187，requests-6028，pylint-4970/7080/8898，pytest-6197/7236，
sklearn-14087/25102，sphinx-11510/7985/9229，sympy-12419/14248/18698。

* 16 个打满预算（llm 51 轮，ntools 42~55）：“只读不写”循环。
  例 `sklearn-14087`：36×run + 14×read，根因定位全对
  （`self.multi_class` 应为局部 `multi_class`），但以“编不过”为由只交文字答案，一行没改。
* 6 个提前停（ntools<35）：requests-6028、sklearn-25102、sphinx-11510/9229、
  sympy-18698、pylint-4970，定位失败后早停。

**B. 改了仍挂（37/59）：** edit_file 70 成功 + 1 失败，apply_patch 15 次中 8 失败；
22/37 同样打满 50 步（改错→验证挂→无步数回滚）。
子项：apply_patch 格式误用（matplotlib-24149 传 unified-diff、
sphinx-8595 3 次缺 `>>>>>>> REPLACE`、xarray-6992 4 次全挂），
环境类（astropy-13236 装不上 erfa/numpy、xarray-3993/6992 30s 超时、
matplotlib-24149/8595 `OSError: Argument list too long: 'docker'`）。
零分组 38/59 打满 ≥50 calls（通过组仅 15/41），平均 llm 轮 43.8 vs 35.9。

## 2. 全量 tool call 与 prefer 符合度

v7 总量 4103 次：`run_command 3101（100/100）/ read_file 834（97/100）/`
`edit_file 147（77/100）/ apply_patch 21（10/100）/ write_file 0 / read_output 0`。
通过组 41 task 全部含 edit（77 次，约 1.9/个）——“落盘”是过线的必要条件。

* `run_command`（shell 复现/跑测）：占 75.6%。可见子集约 50% 是
  `grep/find/ls/git log` 探索，~20% repro，~18% pytest。**字面符合、实则代打**：
  harness 没开 `grep_search/glob_files/find_symbol`，探索只能走 shell。
* `read_file`（分页读）：符合；例外 3 个 bypass prefer：
  matplotlib-25775（50 次全 run，用 `sed -n` 代读）、requests-6028、sympy-18698。
* `edit_file`（单点小改）：符合，用脚投票的首选。
* `apply_patch`（批量多文件）：场景对、用法错，8 次校验失败，可修。
* `write_file`（v7 新增，新建/全量重写）：**0/100 零采纳**——无回归也无收益。
* `read_output`（翻 `.outputs/` 溢出）：0/100，未触发或被重跑代替。

## 3. 与 code-v6 对比

* 净 -2pt（41 vs 43），12/100 flip，属噪声范围：退步 7
  （django-12209/14007/15161、matplotlib-25332、requests-1724、
  sklearn-14087、sympy-12419），进步 5
  （xarray-4094/6938、pytest-10051、sklearn-25973、sphinx-9591）；
  双过 36，双挂 49（硬核）。
* 退步模式一致：v7 edit 更少、shell 更多
  （sklearn-14087 3→0 edit，requests-1724 4→1，django-15161 12→2，
  sympy-12419 read 15→4、run 34→46）——探索卡住，不是 write_file 之过。
* 总量 4184→4103（-2%），run 持平，edit 193→147（覆盖 88→77 task）。

结论：v7 `+write_file` non-regression 成立，但零收益。

## 4. Top3 改进（按 ROI，一次一基因）

**T1. `tools.enabled += grep_search, glob_files`（拟 code-v8）**
理由：v7 约一半 run_command 在做 `grep/find/ls` 探索；3 个 bypass-read、
7 个退步里 sympy-12419 等 read 暴跌，都是搜索低效→预算烧光→零落盘。
预期：探索步数减半，22 个“零落盘”中最有救的一批先转化为 edit。
验证：同 eval100 重跑，看 run_command 占比、首 edit 轮次中位、零落盘数。

**T2. 强制 edit→verify 小循环（拟 code-v9：`verification.enabled=true` 或 prompt 加“先落小 patch 再跑定向测试”）**
理由：38/59 零分打满预算、通过组平均少 8 轮；37 个“改了仍挂”多为改错无回滚；
sklearn-14087 类“分析对、不落盘”直接可转分。
预期：把 near-miss（改了但挂）和 no-edit（分析完不改）两类往双过搬。
验证：首 edit 轮次、edit 后 3 轮内跑 pytest 的比例、双挂 49 的收窄数。

**T3. 修 `apply_patch` schema 理解 + 给 `write_file` 真实角色（拟 code-v10：prompt 加最小 SEARCH/REPLACE few-shot，write_file 限定“新建/全量重写”）**
理由：apply_patch 8 次失败全是格式问题（传 diff、缺 REPLACE、拆参数）；
write_file 0/100 说明 schema 自述没改变模型习惯。
预期：apply 失败率→0，多文件题（django/sphinx 系）成功率回升；write_file 要么有采用、要么确认可删。
验证：apply 错误数、write_file 采用率、多文件 task 通过率。
