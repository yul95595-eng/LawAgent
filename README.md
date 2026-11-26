# LawAgent —— 智能法律咨询多 Agent 系统

面向律所场景的多 Agent 智能法律咨询系统：**案件分析、合同审查（HITL 人机协同）、企业合规体检**三大核心业务流，由 Orchestrator 统一做 8 类意图路由，28 个 LangGraph 节点流式执行，法条/类案引用逐条核验；检索环节为受限自主 Agent（≤4 轮），并带「核验→检索」「复核→起草」两条打回闭环。

## 实测数据（均可复现）

| 指标 | 口径 |
|---|---|
| 法条检索（对照实验） | 同一套 TF-IDF 引擎：整段案情当 query，top1 **0.090**、第八十七条不在 top-6；换术语式短查询，top1 **0.343**、排名第一。说明瓶颈在问法不在引擎 |
| 检索命中 | 三路召回融合（术语短查询 / 案由术语包 / 原文保底，每路前 2 必进）上线后，评测集案件组命中 **10/10** |
| 引用可信 | 案件链只允许检索命中的 329 条库内条文回填；合规链对模型写出的条号逐条回库比对，查无此条即剥离 |
| 评测 | 30 题 / 5 组；真实模型 qwen-plus **29/30（96.7%）**，引用准确率 **100%** |
| 测试 | **pytest 221/221 全绿**（解耦守门、检索自主循环、两条打回闭环、HITL 回环、SSE 全流程、权限门禁、数据源适配、运维指标） |
| 端到端 | docker compose 三容器 healthy，**19 项检查全过**；HITL 挂起 → 重启 → 续跑已验证 |
| 多 Agent 闭环 | 检索 Agent 自主决定问法与轮次（≤4 轮，候选<3 不许收口、模型不可用降级固定顺序）；核验缺请求权基础条文 → 打回检索补检；复核发现过强表述/无据结论 → 打回起草（各限 1 次，超限交人审） |
| 数据源可替换 | 工商信息 mock / 企查查 / 天眼查，**换供应商只加一个适配器文件**，Agent 节点零改动 |
| 可观测性 | `/metrics` 暴露 HTTP / 节点 / LLM 三层指标，降级与断流可见 |

## 核心架构

```
用户消息 ──► Orchestrator（8 类意图路由：关键词规则 + 兜底推断 + LLM 兜底）
                │
   ┌────────────┼───────────────┐
   ▼            ▼               ▼
CaseAgent   ContractAgent   ComplianceAgent
(12+1)       (8 节点)         (6 节点)
   │            │               │
   │        partner_review   citation_check
   │        interrupt(HITL)  （引用逐条核验，剥离编造法条）
   ▼            ▼               ▼
        结构化结果卡（AgentResult）+ SSE 逐节点推送
   │            │               │
   └────────────┴───────────────┘
记忆层：ChatThread / ThreadMessage / ContractReview（SQLAlchemy 异步）
工具层（18 个 / 4 类）：
  LLM 类   —— 意图兜底 / 争议焦点 / 胜诉率 / 类案重排 / 意见书生成 …
  规则类   —— 赔偿计算(2N/N+1) / 时效测算 / 合同风险规则引擎 …
  MCP 类   —— 法条检索 / 类案检索 / 联网检索 / 标准查询（FastMCP）
  API 类   —— 案件管理 / 合同管理 / 法规库 / 工商库（httpx 封装）
```

- **8 类意图**：case_analysis / contract_review / compliance_check / limitation_inquiry / precedent_search / legal_opinion / clause_analysis / risk_assessment
- **HITL**：高风险合同 `interrupt()` 挂起 → 合伙人在右侧面板确认/退回 → `Command(resume)` 恢复图执行；退回自动回环重写意见书再提审
- **引用核验**：合规报告全部法条引用逐条对库校验，演示实测准确率 100%，编造条文直接剥离标记
- **先问再答**（`clarify_slots` 条件分支）：每个案由定义 3~5 个关键槽位，缺关键信息且用户只写了一句话（<50 字）→ 返回「需补充信息」卡片，**不检索、不出结论、不落库**；已写长案情的只标注缺口不打断。实测把「试用期六个月是否合法」这类需要合同期限才能判的问题从"猜着答"变成"先问"
- **法条检索三路召回融合**：术语短查询（提精度）+ 案由术语包（扩召回）+ 原文长查询（保底），每路保底前两名再按分补足，避免多诉求案子被单一路径垄断；单条采纳分下限 0.08，零命中时明确声明「法条库未收录该领域」而不硬凑条文。实测同一套 TF-IDF，整段案情当 query 时《劳动合同法》第八十七条不在 top-6（0.090），术语短查询下排名第一（0.343）
- **多 Agent 闭环（P1~P3）**：检索子 Agent 看候选缺口决定下一轮问法（术语/术语包/原文/收口，≤4 轮；候选<3 条不许收口、同 query 不重搜、模型不可用按固定顺序降级），结果与旧三路口径逐字段可比；`verify_law_coverage` 核对本案由请求权基础条文（解除→第八十七条等），缺则带缺口提示打回检索补检 1 次；`review_draft` 落库前复核（过强表述 / 无据结论 / 时效缺失），打回起草 1 次，仍不过转 `forced_pass` 标注「存在争议」。开关 `RETRIEVAL_AGENT_ENABLED` 可秒级回滚旧逻辑；检索轨迹带 `actor`（policy/agent/fallback），每步谁决定的都可查
- **完整流程讲解稿**：`docs/AGENT_FLOW.md`（每条链路按「谁做决定 / 卡在哪 / 谁兜底 / 出错会怎样」四段式，附 90 秒口述版与已知短板清单）
- **Agent 解耦**（能力白名单唯一事实源：`backend/agents/capabilities.py`）：每个 Agent 只拥有自己业务域的意图与工具，域内工具两两不相交；`tests/test_decoupling.py` 以 AST 导入扫描 + 独占 token 扫描 + 意图唯一映射三道防线守门。时效咨询（limitation_inquiry）是案件域的确定性能力，由案件域 FAQ 小图（case_faq，单节点规则直出）承接，ComplianceAgent 只做企业合规体检；Agent 间协作只经 Orchestrator 的 pipeline_context 显式传递
- **数据源适配层**（阶段 3）：工商信息走 `backend/tools/adapters/`（mock / 企查查 / 天眼查），判例走 `backend/retrieval/precedent_source.py`（本地 66 条真实案号 / 远端判例库）。上层只认归一化字段，换供应商不改 Agent；返回结果带 `data_source`，**mock 数据绝不会被包装成真实征信**
- **安全合规**（阶段 2）：自助注册只开 lawyer/legal 且落库 pending，合伙人审核放行后才能登录；access 12h + refresh 7d 双令牌，类型门禁保证 refresh 泄漏也调不了业务接口；登录密码错误 5 次/分钟按「账号+IP」限流（不误伤同 NAT 出口的同事），对话 10 次/分钟/用户；手机号/身份证/信用代码/银行卡统一脱敏后才出参
- **可观测**（阶段 4）：`/metrics` 暴露 HTTP 请求量/耗时、Agent 节点耗时、LLM 主备调用结果、HITL 挂起数、SSE 断流数；SQLite/PG 定时备份脚本；Locust 压测脚本按「首事件延迟 / 整轮耗时」分开统计

## 技术栈

- 后端：Python 3.11 · FastAPI · LangGraph 1.0 · SSE（sse-starlette）· SQLAlchemy 2 async · SQLite（开发）/ PostgreSQL（生产）· JWT · MCP 官方 SDK
- 前端：Vue 3 · Element Plus · Vite 6（fetch-ReadableStream 手动解析 POST SSE）
- 模型：通义千问 qwen-plus（DashScope OpenAI 兼容接口），未配 Key 自动降级确定性 Mock，**零配置可运行**

## 快速启动

### 本地（推荐）

```bash
# 1) 后端（端口 9089）
cd LawAgent
pip install -r requirements.txt
# 可选：.env 里配置 DASHSCOPE_API_KEY=sk-xxx 启用真实大模型；不配则 Mock 模式
uvicorn backend.api.app:app --host 127.0.0.1 --port 9089

# 2) 前端（端口 3003，/api 代理到 9089）
cd ../Agent_events/lawagent-web
npm install
npm run dev
# 打开 http://localhost:3003
```

### Docker Compose（一键启动）

```bash
cd LawAgent
docker compose up -d --build      # 首次构建约 5–10 分钟（取决于拉基础镜像的速度）
docker compose ps                 # 三个容器都应为 healthy
# 前端 http://localhost:3003（nginx 托管 + /api 反代 9089）
# 后端 http://127.0.0.1:9089/api/health
```

- **配置来源**：compose 只从宿主机 `.env` 取 `DASHSCOPE_API_KEY` / `JWT_SECRET` / `DEMO_PASSWORD`，镜像里不含任何密钥（`.dockerignore` 已排除 `.env`）。没配 `DASHSCOPE_API_KEY` 时容器内走 Mock 模式，全链路仍可演示。
- **国内网络**：后端 Dockerfile 默认 apt→阿里云、pip→清华源；前端默认 npm→npmmirror。docker.io 拉基础镜像慢时可换源重建：`docker compose build --build-arg NODE_IMAGE=docker.1ms.run/library/node:20-alpine`。
- **数据**：PostgreSQL 数据在 `lawagent-pg` 卷；`docker compose down` 不删数据，`down -v` 才会清库。
- **备份**：`docker compose exec backend python scripts/backup_db.py --pg --out-dir /app/backups`（脚本已装 `pg_dump`）。
- **指标**：容器内 `http://backend:9089/metrics`，宿主机 `http://127.0.0.1:9089/metrics`；公网暴露前设 `METRICS_TOKEN`。

**容器实测结果（2026-11-18，19 项全过）**

| 项 | 结果 |
|---|---|
| 三容器状态 | postgres / backend / frontend 全部 healthy |
| 健康检查 | `llm_mode=llm`、`checkpointer=postgres`、法条 329 条、判例 66 条 |
| 注册审核流 | 注册待审 → 登录 403 → 合伙人放行 → 登录 200 |
| 3MB 文件上传 | 200（nginx `client_max_body_size 20m` 生效，默认 1MB 会 413） |
| SSE 反代 | 首事件 **6ms**，整轮 18.4s（真实 qwen），事件序列 meta→result→done |
| 限流 | 经 nginx 连打错密码 `401×5 → 429`；伪造 `X-Forwarded-For` 无效（nginx 覆写真实来源） |
| HITL 跨重启 | 合同审查挂起 → `docker compose restart backend` → 待审列表仍命中 → 合伙人确认返回 `approved` + 回执 |

## 演示账号（首次启动自动种子）

| 工号 | 角色 | 密码 |
|---|---|---|
| L1001 | 律师（提交业务） | 见 `DEMO_PASSWORD` |
| P2001 | 合伙人（HITL 终审 / 账号审核） | 见 `DEMO_PASSWORD` |
| L3001 | 法务（合规档案） | 见 `DEMO_PASSWORD` |

密码不写死在代码里：在 `.env` 设 `DEMO_PASSWORD` 即用它；**不设则首次启动随机生成，并在日志打印一次**（`docker compose logs backend | grep 随机密码`，或本地启动的控制台）。种子只在账号不存在时执行，已有库不会重置密码。

自助注册（登录页「注册」tab）只开律师/法务两种角色，注册后状态为**待审核**，登录会被 403 拦住 —— 合伙人登录后在右侧面板「账号审核」tab 放行或拒绝即可（审核动作进审计日志）。

## 三个演示脚本（登录后点快捷按钮即可）

1. **案件分析**（CaseAgent 12 节点主链 + 1 追问分支）：违法辞退案情 → 自动抽取工作年限/月薪 → 赔偿金 2N 计算与金额校验 → 争议焦点 → 胜诉率评估 → 真实类案检索 → 法条引用。实测输出：2N = **140,000 元**、胜诉率 **65%**、类案案号、《劳动合同法》第八十七条等 5 条引用。
2. **合同审查 HITL**（ContractAgent 8 节点）：内置演示合同（试用期 6 个月、竞业 5 年无补偿、违约金 10 万等）→ 规则引擎 + LLM 双通道识别 6 处风险 → `interrupt()` 挂起 → 换 **P2001** 登录在右侧「待审意见书」**确认出具** → 图恢复，回执 RCPT-xxx，状态 approved。
3. **企业合规体检**（ComplianceAgent 6 节点）：华创精密对外股权投资 → 工商档案比对 → 风险项 + 风险评分 → 全部引用法条逐条核验（实测 4/4，准确率 100%）。
4. 附：**仲裁时效咨询**（案件域 FAQ 小图）→ 时效规则库直答（劳动仲裁时效 1 年），引用回条文库验真。

## 测试

```bash
cd LawAgent
python -m pytest            # 221 passed（pytest.ini 已配 asyncio auto / 无盘写）
```

覆盖：18 工具单测、3 Agent 全流程、HITL 确认/退回回环、Orchestrator 8 意图路由、MCP 进程内调用、SSE 全事件流、JWT 权限门禁（律师不可审、合伙人可审）、注册审核流（pending→403→approve→登录）、双令牌类型门禁、限流（含「不误伤同 IP 其他账号」）、脱敏、文件解析与 Word 导出、工商/判例数据源适配（MockTransport 打桩，不打外网）、备份脚本与指标端点、JWT 密钥策略（生产缺密钥拒绝启动 / 反代 IP 解析）。

浏览器回归（Playwright + 本机 Chrome，10 项全过）：注册待审 → 登录被拒 → 合伙人「账号审核」列表 → 二次确认放行 → 新账号登录 → 时效咨询 SSE 答复与引用法条渲染。

## 评测中台（最小版）

单测只回答「接口通不通」，评测回答「答得好不好」。四条命令：

```bash
python scripts/eval_run.py --inproc                                  # 进程内 Mock，秒级（CI 用）
python scripts/eval_run.py --base-url http://127.0.0.1:9089 --tag real  # 真实模型全量
python scripts/eval_diff.py                                          # 两次对比：哪题从过变挂
python scripts/eval_run.py --inproc --fail-under 0.75                # 低于阈值退出码 1
```

- **评测集**：`evals/cases_v1.json`，**30 条 / 5 组**（案件分析 10、时效 6、合同 8、合规 4、越界 2），每条带确定性判分口径
- **判分**：引用是否命中期望条文、引用能否在条文库查到（拦编造）、案由是否一致、类案是否命中、合同风险点数量、越界是否守住且不编条文
- **产物**：`evals/runs/<时间戳>-<mode>.json`（逐题断言明细 + 汇总指标），`eval_diff.py` 直接列出回归题
- **门禁**：`tests/test_eval_gate.py` 读最近一次跑分，通过率 <75% / 引用准确率 <95% / 越界未 100% 守住的直接红

基线（2026-11-20，三路召回 + 槽位澄清上线后）：Mock 模式 **27/30（90%）**；真实模型 qwen-plus **29/30（96.7%）**、引用准确率 **100%**。更早基线（2026-11-18，改造前）：Mock **24/30**、真实 **27/30**、均值 **11.4s/题**、P95 **21.8s**——两次基线对比即改动效果的证据。
评测集首轮就抓到 **2 个真问题**（单测全绿也发现不了的那种）：

1. **时效语境判定漏词**：问「竞业限制补偿金没给，主张权利的时效」，关键词表只有「劳动/仲裁/工伤/工资/经济补偿」→ 未命中，按民事 **3 年** 回答；正确答案是劳动争议 **1 年仲裁时效**。
2. **试用期超上限漏检**：1 年期合同约定 6 个月试用期（法定上限 1 个月），合同审查输出 6 个风险点里**没有试用期这一项**（详情见 `HANDOVER.md` 3.6 / 第 5 节）。

## 运维（阶段 4）

```bash
# 1) 指标：Prometheus 直接抓（配了 METRICS_TOKEN 则需 Bearer 头）
curl -s http://127.0.0.1:9089/metrics | head
#    lawagent_http_requests_total{method,path,status}
#    lawagent_http_request_duration_seconds{method,path}   （histogram）
#    lawagent_agent_node_duration_seconds{agent,node}       （哪个节点拖慢流水线）
#    lawagent_llm_calls_total{model,outcome}                （primary_ok/fallback_ok/failed）
#    lawagent_chat_stream_errors_total                      （SSE 已回 200，靠它发现中途断流）

# 2) 备份：业务库 + LangGraph checkpointer 库（HITL 挂起状态在里面）
python scripts/backup_db.py --out-dir ./backups --keep-days 7        # SQLite
python scripts/backup_db.py --pg --out-dir ./backups --keep-days 30  # PostgreSQL（需 pg_dump）
python scripts/backup_db.py --dry-run                                # 只看计划

# 3) 压测：先放宽限流，否则压出来的是限流器不是系统
#    CHAT_MAX_PER_MIN=100000 AUTH_IP_MAX_PER_MIN=100000 启动后端
pip install -r requirements-ops.txt
locust -f scripts/locustfile.py --host http://127.0.0.1:9089 -u 30 -r 5 -t 3m --headless
```

## 生产化路径

- 数据库：`USE_SQLITE=0` 切 PostgreSQL（asyncpg），LangGraph Checkpointer 同步切 PostgresSaver（依赖已锁定）
- 向量库：开发态 TF-IDF 本地检索，生产口径 Milvus（检索接口已抽象）
- 数据源：`ENTERPRISE_DATA_SOURCE=qcc|tianyancha` + 对应 key；`PRECEDENT_DATA_SOURCE=api` + 判例库地址。**未配 key 时回落 mock/local 且 `/api/health` 会标明来源**，运行期调用失败则直接报「查询失败」，不编数据
- 观测：审计日志表已落库（登录/对话/审核/删除全动作）+ Prometheus 指标端点，可接 ELK / Grafana
- 备份：`scripts/backup_db.py` 配 cron（示例见脚本 docstring），恢复 = gunzip 回原文件名 / `pg_restore`
- 限流：进程内滑动窗口，多实例部署切 Redis（`backend/core/ratelimit.py` 接口签名不变）
