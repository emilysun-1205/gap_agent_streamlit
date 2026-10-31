# Literature Gap & Hypothesis Discovery Agent(v0.5.1 · Streamlit 单体应用)

科研文献空白挖掘与科学假设发现智能体 —— 依据《Literature_Gap_Hypothesis_Discovery_Agent_生命科学医学版.pdf》框架构建。

**当前版本 v0.5.1,架构已迁移为 Streamlit 单体应用**:主入口 `app.py` 直接调用原有科研核心 `gap_agent.run_pipeline()`(科研核心零改动);不再依赖 Netlify 前端与独立 HTTP API;支持 Streamlit Community Cloud 免费云端部署。架构迁移详情见 `STREAMLIT_MIGRATION.md`;历史版本:`archive/v0.1/`、`archive/v0.4/`、`dist/gap-agent-v0.5-final.zip`。

## 0. 快速开始(Streamlit 应用,推荐)

```bash
# 1) 安装依赖(Python 3.11)
pip install -r requirements.txt          # requests matplotlib weasyprint python-docx markdown streamlit

# 2) 系统依赖(PDF 报告 + 中文字体;Debian/Ubuntu)
apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
    libharfbuzz0b libharfbuzz-icu0 libcairo2 libcairo-gobject2 \
    libgdk-pixbuf-2.0-0 fontconfig fonts-noto-cjk

# 3) 启动
streamlit run app.py                     # 打开 http://localhost:8501

# 4)(可选)配置 OpenAlex 密钥,提升反向检索覆盖
export OPENALEX_API_KEY="你的密钥"        # 或写入 ~/.openalex_key;不配置也能运行(单源部分覆盖)
```

在页面 **Research Workspace** 输入研究主题 → 设置参数 → 点击「启动科研分析流水线」→ 观察 8 个页签(检索/筛选/深抽/Gap/假设/证据/报告下载)。失败时如实显示错误分类与 traceback,可展开查看详细日志。

**首次测试建议小参数**(max_retrieve=10 / screen_keep=5 / deep_extract=2),完整分析耗时数分钟(含真实网络检索与反向核验)。

## 1. 科研核心架构(3 核心 Agent + 5 支撑模块,六节点闭环)

| 节点 | 模块 | v0.5.1 关键行为 |
| --- | --- | --- |
| Node 0 规划器 | `gap_agent/planner.py` | 概念组命中(中英触发词)驱动检索式;precision/recall/mechanism 式排除综述;contradiction 式保留 meta 分析 |
| Module 1 检索器 | `gap_agent/retriever.py` | Europe PMC;DOI→PMID→规范题名联合去重;Crossref 分日期核验(不一致降级 partial);`yes()` 显式解析 Y/N;预算边界(0 预算→not_executed+budget_exhausted,不假 ok);403 逐层传递;状态白名单契约(合法≠可用) |
| Module 2 评估器 | `gap_agent/evaluator.py` | 综述优先识别(pub_types+结构信号);多模型保留;综述初筛配额 ≤25% |
| Node 1 抽取器 | `gap_agent/extractor.py` | 句类先分类(finding/speculative/negative/review_transfer/method/background/unresolved)后定级;三元组;同句不双计 |
| Module 3 证据库 | `gap_agent/store.py` | 统计纯函数(幂等,按去重论文计);analysis_set 显式口径;stats_summary 统一出口(含 hypotheses_system_pending_review) |
| Node 2 Gap 发现 | `gap_agent/gap_hunter.py` | 三元组冲突判定;英文三级反向检索(narrow→medium→broad);单篇充分解决即停止;收窄实际改写描述;MIN_PAPERS=5 门槛 |
| Node 3 假设架构 | `gap_agent/hypothesis.py` | 系统/作者假设分离(origin);竞争性假设;英文新颖性核查;每个 system 假设生成后即执行 finalize(拟议设计标注+占位符/裸模板扫描,未具体化→pending_review+degraded);非基因实体(exosome/TAM)不套基因敲除模板;重写消费完整候选核验 |
| Module 4 核验器 | `gap_agent/validator.py` | 六项状态分立(预测可区分性/完整性/可检验性/待核查项/整体接纳/专家评审);待审不无条件置通过;问题补齐可升级(留痕);配对判定只统计整体通过假设 |
| 排序器 | `gap_agent/scorer.py` | 逐维 evaluated 标志;F 未评估不参与;部分评分+权重覆盖率+不确定性;无完整总分冒充 |
| 报告器 | `gap_agent/reporter.py` | 数据源状态由日志生成;图表口径=正文口径;人工复核样本;待审假设单独呈现(不混入完整通过);PDF(weasyprint)/Word(python-docx)导出;无候选 Gap 时生成占位说明图(不虚构数据) |
| 编排 | `gap_agent/pipeline.py` | 先注册假设再核验;rewrite 后、recheck 前对 rewritten 重新执行 finalize;失败运行也落盘;空库不崩;可选 progress 回调供 UI 展示真实进度 |

## 2. 两种运行方式

**方式 A · Streamlit 应用(推荐,即开即用)**

```bash
streamlit run app.py
```

**方式 B · 命令行(原有方式,保留不变)**

```bash
python3 -m gap_agent.pipeline "肿瘤缺氧、免疫检查点阻断、耐药" \
    --max-retrieve 120 --screen-keep 40 --deep-extract 20 --out-root runs
python3 -m gap_agent.pipeline "肿瘤外泌体、巨噬细胞极化、转移" --year-from 2018 --out-root runs_topic2
```

**方式 C · 旧版 HTTP 后端(已不推荐,保留备用)**

`webapp/backend.py` 为原 Netlify 前端 + 独立 HTTP API 架构的后端;迁移到 Streamlit 后不再依赖,但文件保留未删除,可 `python3 webapp/backend.py 8090` 启动。详见 `STREAMLIT_MIGRATION.md` 新旧架构对照。

## 3. 输出目录(每次运行)

Streamlit 模式产物写入系统临时目录(`app.py` 页面会显示完整路径并提供 ZIP 一键下载);CLI 模式写入 `runs/<run_id>/`。内容完全相同:

```
<out_root>/<run_id>/
├── search_plan.json        # Node 0 检索式规划
├── papers.json / papers.csv           # 检索+筛选+深抽全量文献(含 relevance/quality/全文状态)
├── evidences.json / evidence_matrix.json / evidence_matrix.csv
├── gaps.json               # 研究空白(含反向检索记录、配对状态)
├── hypotheses.json         # 系统生成 + 作者原文假设(含核验状态、待审项)
├── stats_summary.json      # 统一统计口径
├── report.md / report.pdf / report.docx   # 三格式报告
├── action_items.md / manual_review_sample.md
├── figures/*.png           # 统计图(口径同正文)
├── report_check.json       # 报告一致性核对
├── source_stats.json       # 数据源实际状态
└── run_log.json            # 完整审计日志(密钥脱敏)
```

## 4. Streamlit Community Cloud 免费部署

1. 把本项目推送到你的 **公开 GitHub 仓库**(含 `app.py`、`requirements.txt`、`packages.txt`、`.streamlit/config.toml`、`gap_agent/`);
2. 访问 [share.streamlit.io](https://share.streamlit.io),用 GitHub 登录 → **New app** → 选择仓库/分支,主文件路径填 `app.py`;
3. **Advanced settings → Secrets** 中按需填入 `OPENALEX_API_KEY = "..."`(可选;不填则 OpenAlex 反查按单源部分覆盖执行;密钥不会显示在页面上);
4. 部署完成后获得 `*.streamlit.app` 公网地址。

**平台限制(务必知悉)**:
- **资源**:约 1GB RAM / 共享 CPU;长流水线(完整参数数分钟)在免费资源下可行但偏慢,建议小参数试用;
- **休眠**:约 7 天(一说连续无流量更短)无访问会休眠,下次访问自动唤醒(冷启动约 30-60 秒);
- **无持久化存储**:任务产物在应用容器临时目录,休眠/重启/重新部署后**全部丢失**——结果出来后立即在页面下载(应用内已显著提示);
- **公开性**:Community Cloud 应用默认公开可访问,请勿放置敏感数据;
- **依赖**:`packages.txt` 已声明 WeasyPrint 所需系统库与 CJK 字体,部署时自动安装;若平台安装异常,PDF 导出会失败但 Markdown/数据文件不受影响(页面如实提示)。

## 5. v0.5.1 诚实性边界(重要)

- 系统提出的实验选择一律标注「拟议设计(系统生成,未经原文验证)」,不冒充原文验证结果;
- 无依据模板词(干预手段/条件/参照治疗/模型)标待人工核查并降级设计完整性,不编造;
- 检索无结果/预算耗尽 → not_executed / budget_exhausted,不伪造 ok;
- 小参数下 Gap=0 是 MIN_PAPERS 门槛的诚实行为,不是 bug;
- 待审(pending_review)假设单独呈现,不混入完整通过;
- 所有统计可在 run_log.json 审计;专家评审未进行时如实声明。

## 6. 验证记录

- v0.5.1 全量:16 测试套件通过;两主题零网络重放 ALL PASS(详见 `research-branches/gap-agent-fix-v051/FIX_NOTES_v051.md`);
- Streamlit 迁移(2026-10-30):AppTest 冒烟 5/5(初始渲染 8 页签 / 小参数真实流水线 19s:12→5→2 篇,三格式报告生成 / 事件流 23 条 / 空数据源校验);错误分类 5/5;`gap_agent/` 与 Render Free 已交付包逐字节一致(diff 验证,零改动);详见 `STREAMLIT_MIGRATION.md`;
- **未验证项**:Streamlit Community Cloud 公网部署本身未实测(无平台凭据,由用户自行部署);云端资源限制下的完整参数运行未实测。

## 7. 声明

本 Agent 是科研辅助工具,不替代领域专家判断、系统综述规范、统计咨询、伦理审查或临床决策。所有假设均为待验证假说;报告中标注「待人工核查」的内容必须经领域专家复核后方可使用。

### Qwen 报告分析

完成科研流水线后，在 **Export Reports → Qwen 生成式 AI 报告分析** 点击分析按钮。
模型基于本次报告节选和最多30篇纳入文献的摘要片段，生成中文解读、跨文献比较、Gap复核、假设建议和批判性审查。
结果保存为 `qwen_analysis.md` 和 `qwen_analysis.json`，可单独下载，也纳入本次运行 ZIP。
它不会更改原始证据分级或自动批准模型提出的假设，所有生成内容均待人工复核。

在 Streamlit **Settings → Secrets** 配置（不要提交真实密钥）：

```toml
DASHSCOPE_API_KEY = "你的阿里云百炼密钥"
QWEN_MODEL = "qwen3.8-flash"
# 可选：百炼工作空间的官方兼容接口地址；地域必须与密钥一致。
QWEN_BASE_URL = "https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1"
```

本地可使用同名环境变量。未指定 `QWEN_BASE_URL` 时使用界面选择的北京或新加坡接口。
每次点击仅发起一次模型请求，不自动重试或切换模型。调用会消耗模型额度；如只使用免费额度，请先在百炼控制台开启对应模型的“仅使用免费额度”。
模型输出引用ID必须属于发送的文献集合，否则拒绝保存；这项检查不等同于科学结论验证。
