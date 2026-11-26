# 最小评测中台（evals/）

不是"跑个分"，是四件事：**有版本的数据集、可复跑的跑分、可对比的 diff、会红的门禁**。

## 目录

```
evals/
├─ cases_v1.json        评测集：30 条 / 5 组（改口径请出 v2，不要就地改）
├─ README.md            本文件
└─ runs/                每次跑分产物（JSON，逐题明细 + 汇总指标）
```

## 三条命令

```bash
# 1) 进程内 Mock，秒级，改完代码自检/CI 用
python scripts/eval_run.py --inproc

# 2) 对真实服务跑真实模型（容器或本地 uvicorn）
python scripts/eval_run.py --base-url http://127.0.0.1:9089 --tag real

# 3) 两次对比：哪几题从过变挂、哪几题被修好
python scripts/eval_diff.py
```

只跑某组或前几条（调试用）：

```bash
python scripts/eval_run.py --inproc --only contract,limitation --limit 3
```

低于阈值直接退出码 1（可挂 CI）：

```bash
python scripts/eval_run.py --inproc --fail-under 0.75
```

## 判分口径（全部确定性，不引入 LLM 裁判）

| 断言 | 含义 | 为什么这么定 |
|---|---|---|
| 引用命中 | 期望条文（如「劳动合同法第八十七条」）出现在返回引用里 | 法律场景的核心指标：**结论要能落到具体条文** |
| 引用数量 | 引用条数 ≥ N | 拦住「只给结论不给依据」 |
| 引用可对库 | 每条引用都能在本地条文库查到 | 直接拦 **编造条文**（执业事故级错误） |
| 要素命中 | 关键结论词出现（「违法解除」「二倍工资」） | 语义正确性的低成本代理指标 |
| 案由一致 | `result.case_type` 与素材标注一致 | 路由/分类错了，后面全错 |
| 类案命中 | 检索到的判例里有对应案由 | 类案检索是否真的用上 |
| 风险等级 / 风险点数量 | 合同审查必须识别出高危条款 | 规则引擎是否退化 |
| 越界说明 + 未编造条文 | 越界提问要说明边界，且不得出现「第X条」 | 拒答能力，法律助手不可越界给条文 |

一条用例的**所有**断言都过，才算该题通过（严口径，避免"部分对"掩盖退化）。

## 输出指标

跑分 JSON 里固定包含：`pass_rate`、`citation_accuracy`、`checks_passed/total`、
`avg_latency_ms`、`p95_latency_ms`、`by_group`，以及逐题 `checks[]`（哪条断言挂了）。

对比时会单独列出**回归题**（上次过、这次挂）——这是唯一需要立刻解释的部分。

## 门禁

`tests/test_eval_gate.py` 读 `evals/runs/` 最近一次结果：

- 通过率 ≥ 75%（Mock 基线 80%，留波动空间）
- 引用准确率 ≥ 95%
- 越界组 **必须 100%**（法律场景不设容错）
- 不允许出现 5xx

没有跑分结果时该组测试会 skip，不阻塞纯单测。

## 加题 / 改题

1. 新题追加进 `cases_v1.json`（或复制成 `cases_v2.json` 并同步 `meta.version`）
2. `expect` 至少给一条：`must_include` / `citations_any` / `case_type` / `min_risk_points` / `refuse_keywords_any`
3. 跑 `pytest tests/test_eval_gate.py::TestEvalSetShape` 校验结构
4. 跑一次 `--inproc` 确认新题的断言不是"天生必挂"

## 与单元测试的分工

| | 回答什么 | 在哪 |
|---|---|---|
| 单元/集成测试（171 条） | 接口通不通、流程走不走得完、权限对不对 | `tests/` |
| 评测（本目录） | 答得**好不好**：引用准不准、案由对不对、越界守不守 | `evals/` + `scripts/eval_*.py` |

两者不互相替代：全绿的单测也说明不了"类案检索命中率 92%"。
