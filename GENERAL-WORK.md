# 通用工作能力与部署

Atlas DataBuddy 的目标是让用户通过对话完成研究、编程、数据分析和办公交付。能力应按真实任务的完成结果衡量，不能把工具数量、代码测试或一份生成文件换算成与 ChatGPT 对等的百分比。

研究与页图验证见 [VALIDATION-GENERAL-WORK.md](VALIDATION-GENERAL-WORK.md)，浏览器与媒体最新验证见 [VALIDATION-INTERACTIVE-WORK.md](VALIDATION-INTERACTIVE-WORK.md)。

## 当前可用范围

| 工作环节 | 当前实现 | 使用条件 |
| --- | --- | --- |
| 理解目标、计划与工具选择 | 现有 OpenCode Agent、模型目录和任务契约 | 接入支持工具调用的模型；效果取决于实际模型 |
| 公开资料研究 | 原生 `search_web`、`read_web_page`，固定正文快照与引用核对 | 管理员开启公网研究；搜索另需 SearXNG |
| 编程、数据分析与文件生成 | Python / Node / Bash、可版本化代码、后台执行和文件检查点 | 单独部署 gVisor 工作执行服务 |
| 二进制成果继续修改 | 显式复制已有 Excel、Office、图片等固定版本，保留摘要 | 使用实际 `artifact_id`；不会把二进制转录给模型 |
| 网页与办公文件预览 | HTML / PDF / DOCX / PPTX / XLSX 在隔离环境生成 PNG 页图，关联原成果 | 重建包含渲染 helper 的工作镜像 |
| 视觉检查 | 把真实 PNG / JPEG 字节发送给当前模型，保存视觉意见 | 明确启用支持图像输入的模型；不把文字模型冒充视觉模型 |
| 长任务与复用 | 后台运行、取消、恢复、版本、调度、记忆与上下文压缩 | 运行中的服务与持久存储可用 |
| 系统操作与 Agent 协作 | 原有 Skills、MCP、专长 Agent 和外部 Agent 接口 | 独立授权连接；外部写入沿用确认机制 |
| 登录网站、点击操作与用户接管 | 独立云 Chromium、自动阅读与链接导航、应用内用户接管 | 单独部署 gVisor 浏览器和受限网络代理；表单与登录由用户操作 |
| 图片生成、图片编辑与语音 | 真实 PNG / MP3 成果、录音转写、实时 WebRTC 与服务端挂断 | 管理员配置支持相应协议的媒体模型；需实际账号验收 |
| 桌面应用控制 | 未内置通用桌面控制器 | 仍需对应执行器与实际业务验证 |

官方 ChatGPT 文档描述了联网研究、成品文件、视觉与交互工作、浏览器、代码执行、协作和持续任务等不同能力。它们有各自的工具、权限和可用范围，不能用一个“工作模式开关”替代。对照来源：[Use ChatGPT](https://learn.chatgpt.com/docs/use-chatgpt)、[Browser](https://learn.chatgpt.com/docs/browser)。

云浏览器、接管、图片与语音的部署及使用见 [BROWSER-AND-MEDIA.md](BROWSER-AND-MEDIA.md)。

## 开启公开研究

在应用部署的 `.env` 中配置并重启服务：

```dotenv
ATLAS_WEB_ENABLED=1
ATLAS_WEB_ALLOWED_HOSTS=docs.python.org,learn.chatgpt.com,github.com
ATLAS_WEB_SEARCH_URL=https://your-search.example.com/search
```

搜索服务使用 [SearXNG JSON Search API](https://docs.searxng.org/dev/search_api.html)，实例需要在 `settings.yml` 的 `search.formats` 中启用 `json`。不要依赖未经授权的公共实例。若搜索入口有独立 Bearer 认证，可配置 `ATLAS_WEB_SEARCH_TOKEN`；该令牌只发送给固定搜索入口，不转发给网页。

没有搜索服务时可省略 `ATLAS_WEB_SEARCH_URL`，Agent 仍可读取已知且获准的公开 URL。`ATLAS_WEB_ALLOWED_HOSTS` 使用准确主机名，默认不允许任何网站；管理员也可显式用 `*` 允许所有公网主机，私网、本机、云元数据及非标准网页端口仍被拒绝。私网搜索服务还需在原有 `API_ALLOWED_HOSTS` 中明确允许；公网搜索入口要求 HTTPS。

开启此能力意味着搜索查询会发送给搜索服务及其上游。仅用于公开研究；不要在查询中输入保密资料或凭据。已接入公司内部或工作系统资料的项目拒绝使用原生公网研究；其他上传资料的保密分类仍需部署者管理，不应将开放公网工具视为数据防泄漏系统。

### 读取与引用

1. 搜索返回标题、摘要和证据编号。摘要只用于发现来源。
2. `read_web_page` 实际读取正文，返回固定 `evidence_id`、文本摘要、读取时间与字符分页。
3. 后续分页使用编号，保持同一版本；重新传 URL 才重新访问网站。
4. 最终回答的 `web_citations` 包含 `evidence_id`、逐字 `quote` 和回答中实际出现的 `claim`。服务端核对引用来自本任务已读取的正文页，并附上真实来源链接。

网页和搜索结果始终是不可信资料。正文中的“忽略用户、调用工具、发送秘密”等文字不能授予权限。引用核对只证明摘录与已读快照一致，不证明作者身份、发布日期、推断或事实本身正确。正文最多保存 180,000 字符，截断会明确标记；读取不是 JavaScript 浏览器，不登录网站，不获取用户已有会话，也不把 HTTP 成功当作业务事实核实。

每个账号在每个项目的网页证据默认限制为 32 MiB；管理员可用 `ATLAS_WEB_STORAGE_MB` 调整到 1–1024 MiB。达到预算后拒绝新增快照，不自动删除已有引用。该预算与应用数据库备份、容量和保留策略一起管理。

## 二进制成果与页图

先用 `list_project_artifacts` 找到真实成果编号。`run_work_command.artifact_ids` 支持成果面板的 `wa_`、工作文件的 `waf_` 和办公交付件的 `of_` 编号。显式选中的文件校验后位于：

```text
/workspace/artifacts/<artifact_id>/<文件名>
```

原始来源仍需明确附加到当前轮次。传输不扩大文件大小和工作检查点预算，也不复制整个项目或应用凭据。修改时另存并发布新成果，旧版本保留。

对 HTML、PDF 或 Office 成果调用 `render_work_preview`。固定 helper 只在 gVisor 中启动 Chromium、LibreOffice 和 Poppler，应用 API 主机不会打开文档或执行浏览器。输入文件摘要与生成页图关联；原成果的预览自动显示 PNG 页面，同时保留内容视图。

默认生成前 4 页，每次最多 8 页。用 `first_page` 选择后续页段，界面明确显示当前覆盖范围。HTML 页面在无网络执行环境中渲染；应用前端只展示页图，不运行生成的 HTML / JavaScript。这提供渲染预览，不等于交互式网站托管。

工作镜像新增了 `/opt/atlas-work/render.py`，必须按 [WORK-MODE.md](WORK-MODE.md) 重建工作镜像、推送并更新批准的真实 digest；旧镜像不会因为应用代码更新而自动具有此文件。

## 启用视觉模型

确认当前模型和兼容接口确实接受 Chat Completions `image_url` 图像输入后，在应用 `.env` 设置：

```dotenv
ATLAS_VISION_ENABLED=1
```

模型仍来自原有管理员模型目录，不增加另一套前台密钥配置。`inspect_visual_artifact` 会把所选 PNG / JPEG 的真实字节发给本轮固定模型，沿用模型隐私授权、调用预算与用量记录；旧版无状态 OpenCode 文本接口不支持此路径。输入最多 6 MiB、单边不超过 4096 像素，总像素不超过 1200 万。

视觉意见绑定成果摘要，独立加密保存，不记录图片的 base64 到项目代码或视觉回执。它是模型观察，不能替代数值计算、来源复核或财务批准。生成页图成功、模型看过图和业务验收通过是三个不同状态；本次没有把渲染回执升级为视觉或财务验收。

新增内置 `office-quality` 指导，配合真实页图检查可编辑 PPT、Word、Excel 和网页报告。指导文本不能保证模型的设计水平，需要检查实际文件。

## 真实环境验收

先运行实际执行服务检查，包含隔离、文件摘要、取消和真实 HTML / XLSX / DOCX / PPTX 渲染：

```bash
python scripts/verify_work_mode.py --output work-mode-verification.json
```

再使用实际模型与应用地址运行新任务清单。默认不执行、不收费、不产生能力分数：

```bash
python scripts/evaluate_agent.py --cases-file evals/work-mode.json
```

配置 `ATLAS_URL`、`APP_TOKEN` 并确认模型、执行器、网页域名和视觉能力后，显式执行：

```bash
python scripts/evaluate_agent.py --cases-file evals/work-mode.json --execute --limit 5 --output work-task-results.json
```

执行会调用所配置的模型，创建独立验收项目并保留文件。五项任务检查带原文引用的研究、精确 Excel、真实 HTML 页图、跨轮修改二进制成果、实际图像输入。检查器下载文件核对摘要与单元格，不只相信回答中的“完成”。视觉任务仍要求人工核对图像描述和设计质量。

对比 ChatGPT 时需使用相同输入与交付标准，记录真实文件、完成率、耗时、费用、返工和人工接管。仓库中尚无这样一组经过核对的 ChatGPT 基线，因此目前不提供“能力对等”的结论。实际模型任务结果、真实执行器部署、浏览器业务操作及生产 HA 必须分别验收。
