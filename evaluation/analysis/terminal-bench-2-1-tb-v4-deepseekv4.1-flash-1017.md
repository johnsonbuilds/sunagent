# Terminal-Bench 2.1 × tb-v4 全量 memory 门控验证报告

- job: `jobs/terminal-bench-2-1-tb-v4-deepseekv4.1-flash-1017`（89/89 trials 完成）
- harness: `harnesses/tb-v4.yaml`（context_budget 60k tokens，preserve 8k chars，summary cap 2048，hard-gate 2x）
- 模型: deepseek-v4.1-flash（1M 上下文，OpenRouter）
- 代码版本: 含 `812a9e1`（memory context gates）及后续 5 个修复 commits
 （`9291f51` review 修复 ×8、`d1f5b70` boundary 冻结修复、`e9c7af5` 回退观测 + 60k 预算、`6f72e05` invalid-retry），
  以及其后的 `memory→context` 改名重构（语义经 grep 逐行确认保留）。
  注意：本次运行的事件名为 `context.*`（改名后），分析脚本同时兼容 `memory.*`。
- 模型: deepseek-v4.1-flash（1M 上下文，OpenRouter）
- 结果: **69/89 通过（0.775）**，4107 次 LLM 调用，33 个任务触发过 compaction，27 个产生过真快照。

## 1. 各改进点的生产验证结论

| 改进 | 生产证据 | 结论 |
|---|---|---|
| Usage 锚点（stream 收割 + 回放口径） | 45 次 yield 的 `char_budget` 反推比率 2.08–3.96，中位 2.91，**没有一次是默认 4.0** | ✅ 89 任务全量生效 |
| Verbatim 保全（user_preserve） | 45 次 yield 里 31 次带 preserved users（69%），最多一次 14 个（adaptive-rejection-sampler） | ✅ 重度使用 |
| 增量 summary + boundary | **frozen=0/89**（无 md5 复用）；多 yield 任务 boundary 全部严格单调递增；输入规模首压大、后续小（如 video-processing 54→21→20→36 msgs） | ✅ r1 的冻结 bug 已根除 |
| invalid-retry（1 次） | 87 个 flaky episode 里救活 14 个（16%），分散在 14 个不同任务 | ✅ 有用，天花板明显 |
| error/invalid 观测事件 | 160 invalid 全部分类（73 空回包 / 70 缺§2 / 17 缺§3），error 零次——传输层清白 | ✅ 靠它定位了 §3 的新问题 |
| prompt 透传 | tb-v4 未配自定义 prompt，生产零覆盖；`apply_context_strategy` 唯一调用方 `refresh_view` 从不传 prompt，该基因位目前恒为 None | ⚠️ 仅单测 |
| Hard gate（force-budget + refresh hook） | **4107 次调用里 `context_window` 重试零次**——60k 预算相对 1M 上下文太宽，门一次没开过 | ⚠️ 仅单测，生产未验证 |

附带：7 个任务各吃满 6 次 `provider_error` retry（6/7 最终 reward 1.0），恢复基因在生产真实开火；fallback 的 compact 中位压到原文 0.38，保底有效；
`regex-chess`（trace 278M）全程 0 真快照、纯 compact 跑完 121 轮——保底链路的极限压力测试。

## 2. 新发现：input-balloon 恶性循环

invalid 输入规模：中位 17.7 万字符，145/160 超过 12 万字符，最大 54.8 万（`make-mips-interpreter`，41 次 invalid 只成 1 次）。
链条：首次失败（flaky 空回包，不分大小）→ boundary 不动 → segment 越攒越大 → 下次输入更大 → 更容易空/乱 → …。
成功 yield 的输入多在 12–124 msgs，说明 cap 设 ~150k 字符（oldest-first 截断）只勒病态尾巴、正常 case 几乎不触发。
截断需记进 trace（截前/截后长度）。注意：这比“换更强模型”便宜，且与“小模型”方向兼容——输入小了，小模型才可能稳。

## 3. compaction 与 reward 的关系

重 compaction 任务有过有挂（video-processing/path-tracing 过，mips/dna 挂）：memory 决定 run 不死，不决定题做对，符合预期。
6 个 `AgentTimeoutError`（infra 超时，其中 2 个 verifier 仍给 1.0）与 memory 无关。

## 4. 逐任务明细

列：iters（llm.start）/ compact（apply 事件）/ yield（真快照）/ inval（空/缺§2/缺§3）/ retry救活 /
preserved（带 verbatim 用户的 yield 数）/ frozen（md5 复用）/ boundary（多 yield 是否单调）/ reward。

| task | iters | compact | yield | inval(E/缺2/缺3) | retry救活 | preserved | frozen | boundary | reward |
|---|---|---|---|---|---|---|---|---|---|
| adaptive-rejection-sampler | 66 | 1 | 1 | 0/1/0 | 1 | 1 | no | - | 1.0 |
| bn-fit-modify | 31 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| break-filter-js-from-html | 10 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| build-cython-ext | 93 | 1 | 0 | 0/2/0 | 0 | 0 | no | - | 1.0 |
| build-pmars | 14 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| build-pov-ray | 18 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| caffe-cifar-10 | 12 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| cancel-async-tasks | 44 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| chess-best-move | 11 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| circuit-fibsqrt | 23 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| cobol-modernization | 56 | 3 | 0 | 0/6/0 | 0 | 0 | no | - | 1.0 |
| code-from-image | 68 | 2 | 2 | 0/0/0 | 0 | 1 | no | mono | 1.0 |
| compile-compcert | 16 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| configure-git-webserver | 8 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| constraints-scheduling | 19 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| count-dataset-tokens | 18 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| crack-7z-hash | 73 | 2 | 1 | 1/1/0 | 0 | 1 | no | - | 1.0 |
| custom-memory-heap-crash | 12 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| db-wal-recovery | 13 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| distribution-search | 16 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| dna-assembly | 101 | 7 | 3 | 4/6/0 | 2 | 2 | no | mono | 0.0 |
| dna-insert | 48 | 1 | 1 | 0/0/0 | 0 | 1 | no | - | 1.0 |
| extract-elf | 8 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| extract-moves-from-video | 40 | 1 | 0 | 0/2/0 | 0 | 0 | no | - | 0.0 |
| feal-differential-cryptanalysis | 17 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| feal-linear-cryptanalysis | 14 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| filter-js-from-html | 74 | 3 | 2 | 1/2/0 | 1 | 2 | no | mono | 0.0 |
| financial-document-processor | 62 | 1 | 1 | 0/0/0 | 0 | 1 | no | - | 1.0 |
| fix-code-vulnerability | 7 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| fix-git | 20 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| fix-ocaml-gc | 41 | 1 | 1 | 0/0/0 | 0 | 1 | no | - | 1.0 |
| gcode-to-text | 95 | 5 | 3 | 2/3/0 | 1 | 2 | no | mono | 0.0 |
| git-leak-recovery | 16 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| git-multibranch | 18 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| gpt2-codegolf | 119 | 6 | 4 | 1/4/0 | 1 | 1 | no | mono | 0.0 |
| headless-terminal | 34 | 1 | 1 | 0/0/0 | 0 | 1 | no | - | 0.0 |
| hf-model-inference | 13 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| install-windows-3.11 | 120 | 3 | 2 | 1/2/0 | 1 | 1 | no | mono | 0.0 |
| kv-store-grpc | 12 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| large-scale-text-editing | 38 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 0.0 |
| largest-eigenval | 92 | 3 | 2 | 1/2/0 | 1 | 0 | no | mono | 1.0 |
| llm-inference-batching-scheduler | 52 | 2 | 1 | 1/1/0 | 0 | 0 | no | - | 1.0 |
| log-summary-date-ranges | 11 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| mailman | 17 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| make-doom-for-mips | 121 | 12 | 1 | 13/8/1 | 0 | 0 | no | - | 0.0 |
| make-mips-interpreter | 121 | 21 | 1 | 27/13/1 | 0 | 0 | no | - | 0.0 |
| mcmc-sampling-stan | 25 | 1 | 1 | 0/0/0 | 0 | 1 | no | - | 0.0 |
| merge-diff-arc-agi-task | 11 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| model-extraction-relu-logits | 78 | 2 | 1 | 2/1/0 | 1 | 0 | no | - | 1.0 |
| modernize-scientific-stack | 13 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| mteb-leaderboard | 16 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| mteb-retrieve | 41 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| multi-source-data-merger | 25 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| nginx-request-logging | 13 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| openssl-selfsigned-cert | 14 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| overfull-hbox | 18 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| password-recovery | 9 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| path-tracing | 42 | 5 | 2 | 2/4/0 | 0 | 0 | no | mono | 1.0 |
| path-tracing-reverse | 42 | 7 | 2 | 4/7/0 | 1 | 0 | no | mono | 1.0 |
| polyglot-c-py | 12 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| polyglot-rust-c | 17 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| portfolio-optimization | 9 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| protein-assembly | 14 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 0.0 |
| prove-plus-comm | 26 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| pytorch-model-cli | 24 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 0.0 |
| pytorch-model-recovery | 21 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| qemu-alpine-ssh | 16 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 0.0 |
| qemu-startup | 55 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 0.0 |
| query-optimize | 8 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| raman-fitting | 22 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 0.0 |
| regex-chess | 121 | 3 | 0 | 6/0/0 | 0 | 0 | no | - | 0.0 |
| regex-log | 17 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| reshard-c4-data | 13 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| rstan-to-pystan | 121 | 1 | 1 | 0/0/0 | 0 | 1 | no | - | 1.0 |
| sam-cell-seg | 58 | 2 | 1 | 1/1/0 | 0 | 0 | no | - | 0.0 |
| sanitize-git-repo | 36 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| schemelike-metacircular-eval | 102 | 4 | 3 | 0/3/0 | 0 | 0 | no | mono | 1.0 |
| sparql-university | 8 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| sqlite-db-truncate | 5 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| sqlite-with-gcov | 8 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| torch-pipeline-parallelism | 8 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 0.0 |
| torch-tensor-parallelism | 9 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| train-fasttext | 28 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 0.0 |
| tune-mjcf | 12 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| video-processing | 93 | 7 | 4 | 1/6/0 | 1 | 0 | no | mono | 1.0 |
| vulnerable-secret | 12 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |
| winning-avg-corewars | 95 | 6 | 1 | 5/5/0 | 0 | 0 | no | - | 1.0 |
| write-compressor | 8 | 0 | 0 | 0/0/0 | 0 | 0 | no | - | 1.0 |

注：`large-scale-text-editing` 本轮 38 轮未触发 compaction（tb-v3 时代 45 轮也未触发——该任务输出被 observation 窗口截后单轮不大）；
`video-processing` 本轮 reward 1.0（此前 tb-v3 与三次单跑均为 0，属任务方差，非 memory 变更所致）。
