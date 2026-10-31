# Literature Gap & Hypothesis Discovery Agent — Streamlit 迁移说明

> 日期:2026-10-30 · 版本:v0.5.1(科研核心) + Streamlit 单体应用层
> 迁移方向:Netlify 前端 + Render Python HTTP 后端 → **Streamlit 单体应用**(app.py 唯一入口)
> 核心原则:**保留科研能力,简化应用架构,改善用户体验,实现免费云端部署**

---

## 一、新增和修改的文件清单

### 新增文件(5 个)

| 文件 | 作用 |
| --- | --- |
| `app.py` | Streamlit 应用唯一入口:主题输入、参数设置、直接调用 `run_pipeline()`、真实进度展示(事件流+进度条)、8 页签结果展示、报告/数据下载、分阶段错误处理与 traceback 查看 |
| `packages.txt` | Streamlit Community Cloud 的 apt 系统依赖(WeasyPrint 的 Pango/Cairo/HarfBuzz + CJK 字体),与 Dockerfile 保持一致 |
| `.streamlit/config.toml` | Streamlit 配置(headless、主题、禁用文件监视器);不含任何密钥 |
| `.streamlit/secrets.toml.example` | Community Cloud Secrets 格式示例(仅占位说明,无真实密钥) |
| `STREAMLIT_MIGRATION.md` | 本文档 |

### 修改文件(2 个)

| 文件 | 修改内容 |
| --- | --- |
| `requirements.txt` | 增加 `streamlit==1.66.1`;原有 6 项科研依赖不动(requests/pytest/matplotlib/weasyprint/python-docx/markdown) |
| `README.md` | 重写为 Streamlit 架构说明:快速开始、两种运行方式、输出目录、Community Cloud 部署步骤与平台限制、诚实性边界、验证记录 |

### 未修改(零改动,已验证)

- **`gap_agent/` 全部 16 个模块** —— 与已交付 Render Free 包(`deploy/gap-agent-backend-free/gap_agent/`)逐字节一致(`diff -r` 验证通过);
- `webapp/backend.py`(旧 HTTP 后端,保留作参考,不再被依赖);
- `tests/` 全部 16 个测试套件;
- `project-site/`(Netlify 前端)、`deploy/`(历史部署包)在原工作区保留不动,未纳入本 Streamlit 包。

---

## 二、原有科研功能保留情况(100% 保留)

**证明方式**:`diff -r deploy/gap-agent-backend-free/gap_agent gap_agent --exclude=__pycache__` → 零差异(逐字节一致);`gap_agent/` 全部文件 mtime 均早于本轮迁移开始时间。

| 功能 | 保留情况 | 说明 |
| --- | --- | --- |
| 文献检索(Europe PMC / OpenAlex / Crossref) | ✅ 零改动 | `retriever.py` 原样;参数 `max_retrieve/year_from/sources` 原样传入 |
| 文献筛选(初筛+综述配额) | ✅ 零改动 | `evaluator.py`;`screen_keep` 原样传入 |
| 深度文献分析(句类分类+证据抽取) | ✅ 零改动 | `extractor.py`;`deep_extract` 原样传入 |
| Research Gap Discovery(三元组冲突+三级反向检索) | ✅ 零改动 | `gap_hunter.py`/`gap_hunter_lex.py`;`max_gaps` 原样传入 |
| Hypothesis Discovery(系统/作者假设+竞争性假设+新颖性核查) | ✅ 零改动 | `hypothesis.py`/`prediction.py` |
| 证据核验与审计(六项状态分立+待审保持) | ✅ 零改动 | `validator.py` |
| 报告生成(Markdown/PDF/Word 三格式+图件) | ✅ 零改动 | `reporter.py`(WeasyPrint+python-docx) |
| 参数定义/数据结构/结果文件格式/引用信息 | ✅ 零改动 | `config.py`/`schemas.py`/`store.py`;papers.json、gaps.json、hypotheses.json、report.md/pdf/docx 等格式与文件名完全不变 |
| `run_pipeline()` 全部科研分析逻辑 | ✅ 零改动 | `pipeline.py`;app.py 只是调用方,不包裹不改写不捕获其内部异常 |
| 诚实性边界(拟议设计标注/待审不冒充/失败不伪装成功) | ✅ 零改动且 UI 层强化 | 流水线 `status="failed"` 时 app.py 如实显示失败分类,绝不显示成功 |

**app.py 对核心仅做两件事**:①调用 `run_pipeline(...)` 并传入用户在 UI 设置的参数;②通过原有可选 `progress` 回调(该回调机制在 v0.5.1 已存在,属科研核心自带能力)读取真实阶段进度。回调异常在核心内部被吞掉不影响流水线行为(核心原有设计)。

---

## 三、新旧架构对照

| 维度 | 旧架构(Netlify + Render) | 新架构(Streamlit 单体) |
| --- | --- | --- |
| 前端 | Netlify 静态站(project-site/,operate.js) | Streamlit 页面(app.py 内置 8 页签) |
| 后端 | 独立 Python HTTP 服务(webapp/backend.py,FastAPI 风格路由) | 无独立后端;app.py 进程内直接调用 `gap_agent.run_pipeline()` |
| 任务提交 | `POST /api/tasks` → 返回 task_id → 轮询 `/api/tasks/{id}` | 点击「启动」按钮 → `threading.Thread` 执行 → progress 回调实时驱动进度条与事件流 |
| 进度展示 | 前端轮询 stage/events 字段 | Streamlit `st.status`+`st.progress`+事件流 dataframe,后端真实事件直出 |
| 结果展示 | 前端 fetch JSON 渲染 | `st.dataframe`/`st.expander`/`st.json` 直读 papers/gaps/hypotheses/evidences.json |
| 报告下载 | `/api/tasks/{id}/report/{fmt}` HTTP 下载 | `st.download_button` 直读产物文件(含 ZIP 一键打包) |
| CORS | 需配置 GAP_CORS_ORIGINS 白名单 | 同源,无需 CORS 配置 |
| 密钥 | Render 环境变量 | 本地:环境变量/`~/.openalex_key`;云端:Community Cloud Secrets(app.py 桥接 `st.secrets`→`os.environ`,不改核心) |
| 错误处理 | HTTP 500/笼统"提交失败" | 分类显示(文献检索失败/分析流水线失败/报告核对失败/外层异常)+ traceback 展开 + run_log 失败事件 |
| 部署 | Netlify(前端)+ Render Free(后端 Docker) | Streamlit Community Cloud 一个应用(免费) |
| 旧文件处置 | — | `webapp/backend.py`、`project-site/` 保留未删(不再被依赖);Netlify 旧站不主动下线 |

**无法直接迁移的功能说明**:无。旧架构的「任务 ID 跨请求查询」在 Streamlit 会话模型下语义不同(会话状态随浏览器会话存续),本迁移以「单会话内启动→查看→下载」呈现;跨会话历史任务查询属 HTTP 后端特性,在单体应用中不再需要,旧后端文件保留可供需要时启用。

---

## 四、本地运行步骤

```bash
# 0) Python 3.11
# 1) 安装 Python 依赖
pip install -r requirements.txt
# 2) 系统依赖(PDF 报告 + CJK 字体;Debian/Ubuntu;其他系统见 WeasyPrint 文档)
sudo apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
    libharfbuzz0b libharfbuzz-icu0 libcairo2 libcairo-gobject2 \
    libgdk-pixbuf-2.0-0 fontconfig fonts-noto-cjk
# 3)(可选)OpenAlex 密钥:export OPENALEX_API_KEY="..." 或写入 ~/.openalex_key
# 4) 启动
streamlit run app.py            # http://localhost:8501
```

命令行方式(原有,保留不变):`python3 -m gap_agent.pipeline "主题" --max-retrieve 120 --screen-keep 40 --deep-extract 20 --out-root runs`

## 五、免费云端部署步骤(Streamlit Community Cloud)

1. 将本包内容推送到你的 **公开** GitHub 仓库(须含 `app.py`、`gap_agent/`、`requirements.txt`、`packages.txt`、`.streamlit/`);
2. 打开 [share.streamlit.io](https://share.streamlit.io) → GitHub 登录 → **New app** → 选择仓库与分支 → Main file path 填 `app.py` → Python version 选 3.11;
3. **Advanced settings → Secrets** 填入(可选):
   ```toml
   OPENALEX_API_KEY = "你的密钥"
   ```
   (app.py 会桥接到环境变量;不配置则 OpenAlex 反查按单源部分覆盖执行,其余功能不受影响);
4. Deploy → 获得 `https://<你的应用>.streamlit.app` 公网地址;此后每次 push 到该分支自动重新部署。

**平台限制(免费,务必知悉)**:
- 资源约 1GB RAM/共享 CPU:完整参数流水线可行但慢,建议先用小参数;
- 无持久化存储:**任务产物是容器临时文件,休眠/重启/重新部署后全部丢失**——结果出来立即在页面下载(app 内多处提示);
- 应用公开可访问,勿放敏感数据;Secrets 不回显;
- `packages.txt` 声明的系统库(WeasyPrint+字体)由平台自动安装;若安装失败,PDF 导出不可用但 Markdown/数据下载不受影响(页面如实提示)。

---

## 六、测试结果及尚未解决的问题

### 已实测(本地,2026-10-30)

| 测试 | 结果 |
| --- | --- |
| `python3 -m py_compile app.py` | PASS |
| Streamlit headless 启动 + `/_stcore/health` | PASS(返回 ok) |
| AppTest 初始渲染(8 页签、侧边栏、参数控件) | PASS |
| AppTest 小参数真实流水线(topic=肿瘤缺氧…,10/5/2/2020/3) | PASS:19s,真实检索 12 篇→纳入 5→深抽 2;report.md/pdf/docx 全部生成;事件流 23 条;`status=ok` |
| 失败分类函数(检索失败/报告核对失败/流水线异常/未知) | 5/5 PASS |
| 空数据源校验(清空 sources → 明确错误提示) | PASS |
| `gap_agent/` 与 Render Free 已交付包 diff | 逐字节一致(零改动) |
| 离线回归(test_v051_validator_pending_review + test_v03) | 3 passed |
| 小参数 Gap=0 | 确认为 MIN_PAPERS=5 门槛的诚实行为(非 bug) |

### 未验证 / 已知限制(如实声明)

1. **Streamlit Community Cloud 公网部署未实测**:沙箱无平台凭据,部署由用户执行;云端依赖安装(packages.txt)、资源限制下完整参数运行均未实测;
2. **长任务会话依赖**:app.py 以浏览器会话为生命周期,关闭页面后运行中断(Streamlit 单体应用固有;旧 HTTP 后端支持后台任务不受会话影响,文件保留);
3. **多用户并发**:Community Cloud 单进程多会话,同时多人启动流水线会共享 CPU/内存,免费资源下不建议并发大参数;
4. **PDF 在云端依赖 packages.txt 正确安装**:未实测,失败时 UI 如实提示文件未生成;
5. Netlify 旧站与 Render Free 包不受影响,但本轮未重新部署(非本轮范围)。
