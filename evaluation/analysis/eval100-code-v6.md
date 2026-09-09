# eval100-code-v6 分析报告

* Job: `jobs/eval100-code-v6-1/`（100 题，4 并行，`code-v6` = code-v4 + max_iterations 50）
* Wall: 2026-09-08 13:27 → 18:03（4.6h）
* 数据集：`evaluation/swe_bench/eval-100.json`（smoke-10 + dev-50 + 40 道新题），
  难度按 manifest 人类预估修复时长：低 `<15min` / 中 `15min-1h` / 高 `1-4h+`
* Trial 中位 7.5min，均值 10.6min，最长 62.8min

## 1. 总分：43/99 = 0.43（3 errored 另计）

3 个 errored 与模型无关：2×`AgentTimeoutError`（pylint-4661/4970，
agent 3600s 超时）、1×`VerifierTimeoutError`（sphinx-7590，测试套件超 1800s）。

## 2. 难度分层（单调，proxy 被实测验证）

| 档 | 通过 | 通过率 |
|----|------|--------|
| 低（37 题） | 22 | 0.59 |
| 中（43 题） | 19 | 0.44 |
| 高（19 题） | 2 | 0.11 |

高档通过的 2 题都是 django（13449 smoke + 14007）。该分层以后可当正式难度标签用。

## 3. smoke-10 子集：7/10，与历史最佳完全一致

通过：astropy/django/pylint/psf/pytest/sklearn/sympy；
挂：sphinx（f2p 0/2）、matplotlib（f2p 1/1 + p2p 71/81，修对目标但坏回归）、
xarray（f2p 0/1）。失败签名与前 5 轮 smoke 一致，可复现。

## 4. Verifier 健康：干净

* 无 insane totals（81/25470 类 bug 零复发）。
* exit 分布：`0:40 / 1:55 / 2:3 / 4:1`，均为正常语义。

## 5. 工具与熔断

* 调用量：`run_command 3109 / read_file 859 / edit_file 193 / apply_patch 23`
  （edit:patch ≈ 8:1，模型用脚投票选 edit_file）。
* 全 run 4184 次工具调用仅 20 次错误（0.5%）：
  `unexpected_arg 8 / unterminated 3 / bad_header 3 / 匹配歧义 2 / edit mismatch 4`。
* `loop.guard` 触发 3 次（matplotlib-26113、xarray-4094、sphinx-8595），
  全是 apply_patch 连错、`consecutive=3` 精确触发。

## 6. 失败分类（56 fail，下阶段金矿）

| 类别 | 数量 | 含义 |
|------|------|------|
| 没打中目标、无回归 | 24 | 纯能力不够（含 sphinx 类 0 编辑） |
| 没打中 + 带回归 | 18 | — |
| **目标修对、回归挂了** | **10** | 如 xarray-3993（f2p 2/2，p2p 2374/2398 仅坏 24 个） |
| **多测目标部分拿下** | **4** | 如 astropy-13977（12/20）、django-13212（3/5） |

后两类 14 个 near-miss 是离 0.43→0.55 最近的路。

## 7. max_iterations=50 裁决：零误伤

* 43 个通过里首个成功编辑最晚第 38 轮（中位 14）；9 个通过跑到 51 轮是验证尾巴。
* 46/100 烧到天花板（51 = 50 轮 + 总结轮），其中 36 是 fail；fail 里 12 个全程
  0 次成功编辑——加轮数也救不了，cap 省的是纯浪费。

## 8. 镜像 GC：满分

100 个镜像拉过，最终只剩 1 个；磁盘 55GB → 80GB 可用；无误报。

## 9. 后续（按 ROI）

1. 14 个 near-miss → edit→test→fix 小循环（见下节 q&a 展开）。
2. 高档 2/19 → 换模型或放掉；别在 smoke 上继续调参（过拟合风险）。
3. sphinx 全灭 → 单独立项（连 patch 都不写，与编辑器无关）。
