# CLM（Context-as-File）L2 实验报告：`tb-v2-selfctx` 全量验证失败

> Job: `jobs/terminal-bench-2-1-tb-v2-selfctx-deepseekv4.1-flash-1017`
> 对照：`jobs/terminal-bench-2-1-tb-v3-deepseekv4.1-flash-1017`
> Model: `deepseek/deepseek-v4.1-flash-1017`（两组同模型；flash 虽轻量但公开评测显著强于 Qwen3.6-27B，
> 本次结论不受"模型太弱"影响）
> Harness: 实验组 `tb-v2-selfctx`（`memory.strategy=self_managed_file`，derive 自 `tb-v2`，gene diff 仅
> memory.strategy + prompt 追加 `CONTEXT FILE` 指引）；对照组 `tb-v3`（= tb-v2 + 仅改
> `provider_error.max_retries 3->6, base_delay 0.5->2`，prompt/tools/memory/verification 一致，可比）
> 状态：**已 revert**（commit `57b5f6e`，revert 了 `5a752a5` 的 13 个文件）。本报告沉淀失败结论。
> Clean-room：仅借鉴 CLM 论文思想，未拷贝 `context-language-models`（CC BY-NC 4.0）任何代码/文本/格式。

## 0. 总体：-9pts，方向判定失败

| | tb-v3（对照） | tb-v2-selfctx |
|---|---|---|
| mean reward | **0.719**（64/89） | **0.629**（56/89） |
| AgentTimeoutError | 12 | **5** |
| 纯失败（reward=0，无异常） | 13 | 28 |
| llm.start 总数 / 中位 | 4256 / 40 | 5255 / 45 |
| llm>100 的长尾 trial | 12 | 24 |
| 跑满 ≥115 iters 的 trial | 3（extract-moves-from-video 121、qemu 121、install 116） | 18（含 build-cython-ext、caffe-cifar-10、filter、fix-ocaml-gc、git-multibranch 等 15 个 121 跑满） |

翻转：丢 15 / 得 7 / 同对 49 / 同错 18。

- 丢：bn-fit-modify, dna-insert, feal-differential-cryptanalysis, filter-js-from-html,
  financial-document-processor, fix-ocaml-gc, git-multibranch,
  llm-inference-batching-scheduler, mcmc-sampling-stan, mteb-leaderboard,
  mteb-retrieve, raman-fitting, sanitize-git-repo, torch-pipeline-parallelism,
  winning-avg-corewars
- 得：adaptive-rejection-sampler, caffe-cifar-10, cobol-modernization,
  feal-linear-cryptanalysis, gpt2-codegolf, sam-cell-seg,
  schemelike-metacircular-eval

唯一正信号：error 12→5（超时更少）。但纯失败 +15 压倒了它。

## 1. 机制侧：L2 实现本身是健康的（非实现 bug）

自研过程曾抓到并修复过一个真 bug（workspace 文件滞后被误判为模型编辑，导致
`syncs≈tools` 自激振荡；修法：turn-start render 基线 + metadata 剥离 + stale-file
section-prefix guard + receipt view-only，回归测试覆盖）。修复后的 full suite 数据证明机制健康：

- 62/89 trials 有 ≥1 真 sync，共 362 次；全部由 `execute_code(python)` 按 header 切片触发
  （prompt 指引路径），单次压缩 -69%~-88%（如 mteb-retrieve iter22：44669→10763 chars；
  dna-insert iter28：44002→6195）。
- 压缩后执行链不断：mteb-retrieve sync 后又跑 30+ tools（read/write/edit 正常），
  dna-insert sync 后又跑 30 tools。
- `syncs << tools`（如 fix-git 19→0，extract-elf 51→5），无 `rejected_broke_pairing` 协议错，
  verification grounding 全程正常。
- 用量门控真工作：单 trial 最多 112 次 `memory.self_managed_file` nudge 事件。
- rollback **0 次**：89 trials 无一次撞 `strict` 墙——主动压缩把水位一直控住了，
  止血链（ledger/final-turn）零覆盖。

即：**输分不是误触发/污染/协议错造成的，是压缩保真度问题 + 任务不匹配。**

## 2. 丢分归因（以 trace + verifier 产物为准）

> 方法：逐个检查 15 个丢失任务的 `<trial>/result.json`
>（`verifier_result.rewards.reward`、`exception_info`）、trace 的
> `verification.passed/failed` 事件、`verifier/test-stdout.txt` 尾部断言。
> 结论先行：**14 个正常结束的丢失任务里，没有一例是 harness submit 校验误杀**——
> harness 的 submit 门（`verification.passed`）与 benchmark 的 reward 判定
> （verifier 容器内跑测试）是两套独立判定。报告初版"压缩洗掉 evidence 导致
> submit grounding 不过"的说法是错的，特此收回。

### 2.1 submit 通过、但 benchmark 判 0（8 个：真实任务失败，与 harness 无关）

这 8 个 trace 里都有 `verification.passed`（submit 门已过），reward=0 来自 verifier
容器内测试断言失败：

| 任务 | verifier 结果（test-stdout 尾部） |
|---|---|
| `bn-fit-modify` | 7 passed / 2 failed：学到的 DAG 结构断言挂 |
| `dna-insert` | 引物 Tm 差 7.95 > 5（`assert abs(fwd_tm - rev_tm) <= 5`） |
| `feal-differential-cryptanalysis` | `attack.py: ValueError: no key5 candidate consistent` |
| `mcmc-sampling-stan` | 5 passed / 1 failed（hierarchical model 未实现） |
| `mteb-retrieve` | `result.txt` 写成 HumanEval 那行，期望 MTEB 行（见 2.4 单例） |
| `sanitize-git-repo` | 2 passed / 1 failed（secret 替换断言挂） |
| `torch-pipeline-parallelism` | 2 passed / 2 failed（rank 微差 0.0417） |
| `winning-avg-corewars` | 2/5 对手达标（stone 54% < 75% 等） |

其中 4 个（bn-fit-modify、torch-pipeline-parallelism、winning-avg-corewars、
feal-differential-cryptanalysis）**0 syncs**——模型根本没碰 mirror，
纯属模型能力/单次噪声，与压缩无关。

### 2.2 从没 submit、跑满 121 iterations（6 个：没做出来，不是被 harness 卡住）

`filter-js-from-html`、`fix-ocaml-gc`、`git-multibranch`、
`llm-inference-batching-scheduler`、`mteb-leaderboard`、`raman-fitting`——
trace 最后一条全是
`verification.failed missing=['submit_result'] / budget exhausted without a valid submit_result call`。
harness 只要求"调一次 `submit_result` 即可交卷"，是模型自己 121 轮都没调。
这 6 个与"高频大压缩 + 跑满"高度重合（fix-ocaml-gc 15 syncs/maxcut 87%，
llm-scheduler 14 syncs，mteb-leaderboard 6 syncs，filter 10 syncs），
但因果只能说"相关"（见 2.4 仅有的单例实锤），不能说"压缩导致交不了卷"。

### 2.3 超时类（1 个）

`financial-document-processor`（AgentTimeoutError）。两组各有超时，属环境/网关抖动。
（selfctx 全组 error 更少：5 vs 12，疑似压缩降低单轮 token 从而减少 stream 超时，
但未严格归因。）

### 2.4 压缩洗掉关键信息：mteb-retrieve 单例实锤（可重建）

trace 里 `llm.start` 的 messages 快照就是压缩前后的 view，可直接对比。
以 `mteb-retrieve` iter22→23 的 sync（44669→10763 chars）为例：

- **压缩前（iter22，71 msgs）**：每个 assistant turn 带 `tool_calls`（tc=1~2），
  每个 tool obs 是原文（含 `/app/data.txt` 内容、mteb 版本、encode 报错 traceback、
  各轮 cosine 分数表）。
- **压缩后（iter23，47 msgs）**：所有 assistant 的 `tool_calls` 被抹掉（tc=0），
  tool obs 全变成
  `(archived tool observation …): (tool output archived; see notes in turn 1 / final summary)`——
  **原文一字不留**，只剩一句指向性空话。而被指向的"turn 1 / final summary"
  经查只是一句任务复述（query/data.txt/模型名），**没有把各轮关键数值
  （cosine 分数、第 5 名是哪行）COPY 进去**。

  该 trial 最终失败恰是 `result.txt` 写错行
  （`HumanEval: …` vs 期望 `MTEB: Massive Text Embedding Benchmark`）——
  而第 5 名的判定恰恰依赖被洗掉的分数表。从机制（有损 `apply_mirror`）到结果
  （写错行）的链条在这个单例上是完整的。**但仅此一例，不能推广到全部 15 个**；
  2.1 的 8 个里其余 7 个均有各自独立的任务失败原因。

曾考虑的"verification 关键 observation 永不压缩"护栏被否决：那正是 CLM 批判的
harness 预设（替模型决定什么重要），加了等于退回 `compact_observations` 思路。

### 2.5 反向 7 个得

`caffe-cifar-10`（6 syncs / maxcut 87%）等——长 trial 里水位管理更好反而帮了忙。
说明压缩在部分任务上是正收益，只是不稳定。

## 3. 对最初四个目标的裁决

1. **模型按用量主导管理 ✅（机制成立）**：62/89 trials 自主压缩，nudge 驱动行为属实。
2. **不变量 ✅**：messages 不可变、配对合法、verification 链正常、默认关闭零影响。
3. **长程止血 ❌（未验证且存疑）**：rollback 0 次；TB-2.1 多数任务在窗口内可做完，
   上下文管理不是瓶颈——**任务选型错**。CLM 论文的收益来自 BrowseComp/EdgeBench/24h
   swarm（数百~数千轮），TB 上测不出。
4. **数据闭环 ✅**：362 条真 sync 事件 + 全量水位 trace，可供复用。

附：LLM 调用 +23%（4256→5255）的构成——中位数仅 40→45，典型 trial 开销没涨；
涨的是长尾：跑满 ≥115 的 trial 从 3 个涨到 18 个，差值最大的 10 个 trial
（adaptive-rejection-sampler +88、git-multibranch +84、headless +82…）全是
selfctx 跑满 121 而 baseline 早提交的类型。**压缩降低了"被迫结束"的压力
（view 一直能塞下），模型把省下的预算全花成更多轮次，但多花的轮次没换成更多通过**
（6 个跑满未交卷 + mteb-retrieve 类做错）。baseline 的 `llm_summary` 在水位高时
更快把模型逼向终局（好坏参半：逼出提交，也逼出 error 12 vs 5）。

## 4. 结论：revert 正确，思想未被证伪但在此配置下失败

- **-9pts 的构成**：约一半是噪声（0-sync 短 trial 翻转，如 bn-fit-modify 等 4 个
  0-sync 丢失）；另一半里，8 个 submit 通过但 benchmark 判 0 的全有各自独立的
  任务失败原因（DAG 断言、Tm 超差、attack 失败、精度差等），与压缩无直接因果；
  只有 mteb-retrieve 是"压缩洗信息→写错行"的单例实锤，6 个跑满未交卷与高频压缩
  相关但因果未定。判定：**L2 + deepseek-v4.1-flash + TB 短任务 = 没赢**。
- 这不证伪 CLM 思想：CLM 是全量改写（无协议折损）、论文任务是长程。
  要证伪至少需要"原生级保真 + 长程任务"对齐，而本次（保真、任务长度、skill/RL 调优）
  都没对齐。诚实结论止于"此实现此配置失败"。
- Revert（`57b5f6e`）正确：`self_managed_file` 默认关闭，留着是 dead code + 第 4 个
  memory 分支的维护负担；护栏路线违背初衷，剩下路线（原生保真重写/P2/长程任务）
  投入大，不值得现在继续。

## 5. 若将来重做，前提清单

1. 任务先换长程（水位贴墙、rollback 真触发），TB 上不值得。
2. 保真先行：tool 结构 native 保留或 `[KEEP-PAIRING]` 默认开，而不是先压再说。
3. 先重跑确认噪声基线（翻转任务子集 ×2），再谈优化。
4. P2 skill 进化（用本次 362 条 sync 事件做冷启动）优先于任何手工护栏。
