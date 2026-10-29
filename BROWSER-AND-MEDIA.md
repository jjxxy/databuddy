# 云浏览器、图片与语音

Atlas 已提供这些功能的服务代码、Agent 工具和应用界面。管理员部署浏览器并连接媒体模型后，用户直接在项目对话中描述工作；不需要填写 API 参数或运行命令。配置状态不代表服务或模型质量已经验收。本轮测试与截图见 [验证记录](VALIDATION-INTERACTIVE-WORK.md)。

## 用户怎么使用

| 工作 | 操作与实际产出 |
| --- | --- |
| 浏览网站 | 告诉 Agent 网站地址与目标；它打开真实 Chromium，读取动态页面、跟随普通链接、滚动并保存可引用的正文快照 |
| 登录、验证码、填写与提交 | Agent 请求接管后，打开「工作设置与工具 → 浏览器」。点击「接管浏览器」，点击页图或选择控件输入；完成后点击「交还 Agent」，回到对话发送「继续」 |
| 看网页截图 | Agent 可保存当前真实页图到成果面板，再通过已启用的视觉模型读取图片；正文摘要不冒充视觉观察 |
| 生成或编辑图片 | 例如「生成一张产品介绍海报」，或指定成果面板已有图片让 Agent 修改；真实 PNG 保存在「成果 → 图片与语音」 |
| 录音转文字 | 点击消息输入区「语音 → 开始录音转文字」，结束后核对转写并加入输入框，再手动发送工作 |
| 实时语音 | 点击「语音 → 开始实时语音」，通过 WebRTC 收发真实音频；语音讨论不直接执行项目工具，转写需核对后发送 |
| 朗读 | 例如「朗读这段说明并生成音频」，真实 MP3 可从成果面板下载；语音标记为 AI 生成 |

Agent 对浏览器的自动访问只放行 GET / HEAD。普通链接通过实际锚点 URL 导航，不触发网页点击脚本。按钮、表单提交、验证码和登录交给用户操作。接管期间，Agent 不能读取该会话的新页面或截图；用户交还后重新读取。等待接管不算任务完成。

浏览器是独立的云会话，不继承本机 Chrome、ChatGPT、密码管理器或已有账号登录。每次会话启动独立 Chromium；Cookie 只在本会话中使用，不导出到 Atlas 或模型。生产容器的临时目录是 tmpfs，会话关闭或 30 分钟到期后清除。原始输入不写入动作日志；网页显示内容、正文证据和明确保存的截图仍属于项目资料。

## 管理员部署浏览器

执行主机必须已经安装并正确配置 Docker 的 gVisor `runsc`，参见 [WORK-MODE.md](WORK-MODE.md)。浏览器和应用分别部署；应用不启动 Chromium，也不持有 Docker socket。

先按工作模式文档构建并推送工作镜像，取得真实不可变 digest。下面占位值须替换为自己的镜像地址：

```bash
docker build --build-arg ATLAS_WORKSPACE_IMAGE=registry.example.com/atlas-work@sha256:YOUR_REAL_WORKSPACE_DIGEST \
  -f deploy/browser-runner/Dockerfile.browser -t registry.example.com/atlas-browser:release .
docker push registry.example.com/atlas-browser:release
```

从镜像仓库取得浏览器镜像的真实 digest。在执行主机准备环境变量：

```dotenv
ATLAS_BROWSER_IMAGE=registry.example.com/atlas-browser@sha256:YOUR_REAL_BROWSER_DIGEST
ATLAS_BROWSER_TOKEN=REPLACE_WITH_A_DEDICATED_RANDOM_SECRET_AT_LEAST_32_CHARACTERS
ATLAS_BROWSER_WORKSPACE_ID=YOUR_ACTOR_PROJECT_WORKSPACE_HASH
ATLAS_BROWSER_PORT=8091
ATLAS_BROWSER_ALLOWED_HOSTS=example.org,login.example.org,static.example.org
```

然后启动：

```bash
docker compose -p atlas-browser-project1 --env-file /secure/browser.env -f deploy/browser-runner/compose.yaml up -d --build
```

每个账号与项目组合使用独立容器栈，不跨账号或项目共享浏览器容器。`ATLAS_BROWSER_WORKSPACE_ID` 是该组合的 SHA-256；已登录用户请求 `GET /api/projects/<project_id>/browsers` 可取得自己的 `workspace_id`。服务启动时必须绑定此值，应用也核对服务返回的绑定。不同项目分别使用不同 Compose 项目名、端口、入口与随机密钥。

部署配置强制 `runsc`、非 root、只读根目录、去除 capabilities、CPU / 内存 / PID 限制。Chromium 内层使用 `--no-sandbox`，外层 gVisor 是必需条件；不能删掉 `runtime: runsc` 当作普通容器运行。单个绑定内的两个浏览器会话不是两个独立安全容器，不能将该栈改为跨租户共享服务。

浏览器仅连接内部 Docker 网络，公网流量经独立 egress proxy。代理每次解析并固定目的 IP，拒绝私网、回环、云元数据、组播、保留地址及非标准端口；重定向与子资源仍受规则约束。代理不解密 HTTPS。它没有对外发布端口，内部网络不能共享给其他不可信容器。应用通过受保护的 HTTPS 入口连接浏览器服务在执行主机的 `127.0.0.1:8091`；不要将该服务直接暴露到公网。

在应用 `.env` 中设置并重启应用服务：

```dotenv
ATLAS_WEB_ENABLED=1
ATLAS_WEB_ALLOWED_HOSTS=example.org,login.example.org,static.example.org
ATLAS_BROWSER_URL=https://your-protected-browser.example.com
ATLAS_BROWSER_TOKEN=THE_SAME_DEDICATED_BROWSER_SECRET
```

上述默认地址适合一个项目。多项目时，在应用管理员环境变量中注册每个已部署服务；每项必须同时填写自己的 URL 和密钥，缺项不会借用其他服务的密钥：

```dotenv
ATLAS_BROWSER_ENDPOINTS_JSON='{"<workspace_hash>":{"url":"https://project1-browser.example.com","token":"PROJECT1_DEDICATED_SECRET_AT_LEAST_32_CHARACTERS"}}'
```

应用与执行服务都要批准目标、登录与资源域名。两处均支持显式 `*`，这会扩大到公开站点，不允许私网地址。内部系统资料项目仍禁止原生公网浏览器；其他上传资料的分类由部署者管理。这些规则不是完整的数据防泄漏系统。

单个浏览器服务最多同时运行 2 个会话，每个会话最多 64 个控制请求，30 分钟到期。动作有唯一编号：重复提交不会再次执行，参数冲突会拒绝；断线后先刷新核对，不盲目重放。页面版本变化时拒绝旧控件操作。服务重启会清除会话，不自动重新登录或重放操作。当前不支持网页文件上传下载、保留弹窗、多标签工作或自动跨站表单流程。

## 管理员连接媒体模型

媒体服务使用独立的管理员配置，不把密钥放进前端、提示词或 Git。只填写确实支持对应接口的模型；未填写的功能不会提供给 Agent：

```dotenv
ATLAS_MEDIA_BASE_URL=https://your-compatible-provider.example.com/v1
ATLAS_MEDIA_API_KEY=YOUR_PROVIDER_SECRET
ATLAS_IMAGE_MODEL=YOUR_IMAGE_MODEL
ATLAS_TRANSCRIBE_MODEL=YOUR_TRANSCRIPTION_MODEL
ATLAS_SPEECH_MODEL=YOUR_SPEECH_MODEL
ATLAS_REALTIME_MODEL=YOUR_REALTIME_MODEL
ATLAS_SPEECH_VOICE=alloy
ATLAS_MEDIA_DAILY_REQUESTS=40
ATLAS_MEDIA_STORAGE_MB=256
```

需要管理员允许当前模型读取项目内容，且通过现有公司资料与协作模型权限检查。媒体端点是另一目的地，不继承文本模型的内部资料许可；公司、工作系统及带协作目的地限制的项目当前拒绝此路径，请在独立公开项目使用。其他上传资料的分类仍由部署者管理。

媒体接口固定使用管理员端点，DNS 固定 IP、TLS 保留原始 SNI，不跟随重定向，也不下载模型返回的任意图片 URL。兼容端点使用 HTTPS；本地媒体服务还需在 `API_ALLOWED_HOSTS` 明确批准，沿用应用的本地连接策略。

| 能力 | 实际协议与限制 |
| --- | --- |
| 图片生成 | `POST /images/generations`，要求返回 base64 PNG，核对尺寸、完整解码和 SHA-256；每次生成 1 张，最大 16 MiB |
| 图片编辑 | `POST /images/edits` multipart，发送明确选择的真实 PNG / JPEG 成果字节；保留旧版本 |
| 转写 | `POST /audio/transcriptions` multipart，录音最多 60 秒 / 12 MiB；应用不保留原始录音，转写回执加密保存 |
| 朗读 | `POST /audio/speech`，返回真实 MP3 文件，最大 16 MiB |
| 实时语音 | 服务端 `POST /realtime/calls` 交换 SDP；浏览器通过 WebRTC 收发音频和转写事件，长期密钥不进入浏览器 |
| 结束语音 | 要求供应商返回 `Location` 中的 `rtc_` call ID，并支持 `POST /realtime/calls/{call_id}/hangup`；关闭界面会结束本地音频并请求挂断 |

每次实时语音保存服务端 5 分钟截止时间，API 后台每 5 秒检查并请求挂断；失败保留待处理状态并重试，重启后继续。后台或上游故障会延迟挂断，因此也要在供应商账号设置费用限制。更换媒体服务地址前先结束活动通话。

每天请求上限按账号计算，覆盖生成、转写、朗读与实时连接。请求发送前持久化记录；不确定结果不自动重新计费。它与文本模型 token 预算分开，并非精确美元成本限制。图片、语音、设计质量和时延取决于所配置供应商；任务完成必须核对真实文件，不能只看回答中的“完成”。

本接口依据官方文档实现：[Image API](https://developers.openai.com/api/reference/resources/images/methods/generate)、[File transcription](https://developers.openai.com/api/docs/guides/speech-to-text)、[WebRTC](https://developers.openai.com/api/docs/guides/voice-webrtc?voice-api=realtime)、[Server controls](https://developers.openai.com/api/docs/guides/voice-server-controls)、[Hang up](https://developers.openai.com/api/reference/resources/realtime/subresources/calls/methods/hangup)。兼容供应商必须实际支持这些协议，不能仅凭名称判断支持。

## 验证与边界

协议与权限测试：

```bash
python -m pytest tests/test_interactive_work.py -q
npm ci --prefix tests/ui
npm test --prefix tests/ui
```

使用已安装的 Chromium 运行真实浏览器服务与真实本地网站验证，不调用付费模型：

```bash
ATLAS_BROWSER_EXECUTABLE=/path/to/chromium npm run test:browser-service --prefix tests/ui
ATLAS_BROWSER_EXECUTABLE=/path/to/chromium npm run test:browser --prefix tests/ui
```

服务测试显式设置测试模式，只允许本地受控网站，生产应用拒绝连接该模式。第一项运行真实 Chromium 和真实 HTTP 登录流程；第二项运行真实前端与受控 API，检查桌面 / 手机页面、交还控制与未配置语音状态。DOM 中的麦克风测试使用明确的模拟接口，不代表真实声学效果。

上线前还需在自己的 gVisor 部署上验证网络隔离、真实登录接管，并使用自己的媒体账号验证生成、编辑、录音、WebRTC 与挂断。当前提交没有使用真实付费模型或外部网站账号，也没有在本环境构建、启动生产 Docker / gVisor 服务。桌面应用控制、通用自动表单代理及完整 ChatGPT 能力对等仍未通过验收。
