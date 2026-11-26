# 数据字典（DATA_DICT.md）

> 作用：任何一条法条 / 判例 / 企业数据的「来源、口径、更新方式、是否敏感」都查这里。
> 改动数据文件前先看本表；改完必须更新最后一行「变更记录」。

## 1. 数据集总表

| 数据集 | 文件 | 规模 | 来源 | 更新方式 | 敏感字段 | 负责方 |
|---|---|---|---|---|---|---|
| 法条库 | `data/legal/statutes*.json`（6 个文件） | 原始 348 条 → 加载去重后 **329 条 / 14 部法** | 全国人大网「国家法律法规数据库」（现行有效版本），原文录入 | 人工增量追加；`meta.updated` 记录日期 | 无 | 法务（李法务） |
| 判例库 | `data/legal/precedents.json` | **66 条**（8 类案由） | 中国法院网、最高法指导案例/公报、人民法院案例库 | 人工增量追加；每条须可回溯真实案号 | 当事人已脱敏（仅保留案情） | 律师（张律师） |
| 工商 mock | `data/mock/enterprises.json` | 4 家 | 内部构造，用于离线演示 | 手工维护 | 统一社会信用代码/法人/电话**返回前脱敏** | 开发 |
| 案件 mock | `data/mock/cases.json` | 3 件 | 内部构造 | 手工维护 | 无真实当事人 | 开发 |
| 合同 mock | `data/mock/contract_files.json` | 2 份 | 内部构造 | 手工维护 | 无 | 开发 |
| 业务库（运行时） | PostgreSQL `lawagent` | 表：users / chat_threads / thread_messages / case_analyses / contract_reviews / enterprise_compliance_profiles / audit_logs | 系统产生 | 应用写入 | 工号密码 bcrypt 哈希；审计日志含操作人 | 运维 |
| LangGraph 状态 | PG（`checkpoints*` 表） | 每线程一份 | 应用写入 | 框架自动 | 含会话内容 | 运维 |
| 评测集 | `evals/cases_v1.json` | **30 条**（5 组） | 依 mock 素材 + 真实业务场景人工编写 | 只增不改；要改口径就出 v2 | 无 | 开发 |
| 跑分结果 | `evals/runs/*.json` | 每次一份 | `scripts/eval_run.py` 产出 | 自动 | 无 | 开发 |

## 2. 关键字段口径

### 法条（statutes）
```
law                 法律全称，如「中华人民共和国劳动合同法」；检索时会去「中华人民共和国」前缀
articles[].article_no   条文号，中文数字，如「第八十七条」（引用校验按此精确匹配）
articles[].text     条文原文（非摘要）；`get_article()` 返回比对用
articles[].keywords 检索关键词（BM25/TF-IDF 分词补充）
```
- **唯一键**：`(law, article_no)`。`load_statutes()` 按此去重，所以 348 条原始数据加载后是 329 条。
- **引用校验规则**：`citation_exists(law, article_no)` 查不到即视为「疑似幻觉引用」，报告里必须剥离或标记。

### 判例（precedents）
```
id / case_no        内部 id + 真实案号（案号缺失的数据一律不得入库）
case_type           8 类枚举：labor_termination / labor_probation / labor_non_compete /
                    labor_service_period / contract_sales / contract_equity /
                    contract_lending / ip_license
law_basis           裁判依据条文，供引用核验
source              公开渠道（可回溯）
```

### 工商（enterprises）
```
credit_code / legal_person / contact_phone   返回前统一脱敏（core/masking.py）
risk_hints          风险提示列表，供合规体检比对
data_source         实际生效数据源（mock / qcc / tianyancha），随返回值透出，禁止抹掉
```

## 3. 数据变更流程（三步，别跳）

1. **改文件**：追加而非覆盖；保持既有字段顺序与命名。
2. **跑校验**：`pytest tests/test_retrieval.py`（结构 + 引用可查）→ `pytest tests/test_eval_gate.py`（评测集结构）。
3. **记账**：更新本表「变更记录」，并在 `meta.updated` 写日期。涉及口径变化时，同步升 `evals/cases_v*.json` 版本。

## 4. 已知缺口（诚实记录）

- 法条**失效检测**未自动化：靠人工核对来源，没有「某条已修订/废止」的告警。
- 判例**地域覆盖**不均：劳动类偏多，建设工程/金融类样本少。
- 工商数据仍是 mock（真实 key 未接），`data_source=mock` 会在健康检查和报告里透出。
- 没有字段级血缘：答出某条法条后，查不到它是哪次入库、由谁录入（审计日志只记操作，不记数据版本）。

## 5. 变更记录

| 日期 | 变更 | 影响 |
|---|---|---|
| 2026-11-18 | 初版：法条 329 / 判例 66 盘点入库口径 | 建立基线 |
| 2026-11-18 | 评测集 v1（30 条）上线 | 效果可量化，见 `evals/README.md` |
