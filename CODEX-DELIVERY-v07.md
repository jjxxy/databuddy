# v0.7：上传资料可直接查看（r10）

## 【本轮改动摘要】

版本 v0.7；产品版本仍为 Atlas DataBuddy v0.20.0，便携构建 r10。对应用户“为什么我上传的文件不能打开看”。本轮未联系豆包。

| 文件 | 对应问题及改动 |
| --- | --- |
| `agent_service/static/app.js` | U3：对话附件原先只是文件名 div，没有查看动作。改为可点击、可键盘聚焦的资料按钮，携带消息冻结的版本号；已上传的输入区资料也能查看。查看保留草稿、附件和待确认请求，不提交 AI 任务。 |
| `agent_service/static/workspace-shell.js` | U3：将资料直接接入现有侧面预览器，复用 Excel 页签、分页、文本安全预览及鉴权下载。来源资料只提供“下载原文件”，不显示成果修改/复用操作。处理关闭、切项目、退出及双文件乱序响应。 |
| `agent_service/static/style.css`、`index.html` | U3：按钮的查看提示、悬停与聚焦状态；更新静态脚本版本，避免旧文件卡片脚本缓存。没有增加导航入口。 |
| `agent_service/workspace_artifacts.py` | U3：项目内按来源 ID 和明确 revision 精确查找原件，返回现有预览/下载描述；历史版本不会跳转到新文件，保持摘要校验、鉴权和归档保护。U4：真实文件复验发现 Excel 可选 dimension 缺失导致空表，改用已有 XML 实扫行列数展示及检查预算。 |
| `tests/test_workspace_artifacts.py` | U3/U4：真实原件、历史替换版本、同字节不同版本、权限、缺文件和篡改；无 dimension、错误 dimension、真实空表、精确数值/公式/坐标、预览预算及财经四页签的实际内容。 |
| `tests/ui/source-buttons.cjs`、`source-preview.cjs`、`package.json` | U3：实际 DOM 文件按钮点击与冻结版本；现有预览器页签/分页/文本/下载；切换与关闭期间迟到响应；不发送消息、不修改草稿及附件。 |
| `verification/verify-source-preview-v07.py`、`live-source-preview-v07.py` | U3/U4：最终包空 PATH、新中文目录、local_only、无模型；并核对现有亲测项目的冻结文件及完整下载字节。 |
| `verification/finalize-r10-candidate.py`、`trial-r10-state-check.py` | U4：未交用户的候选包修补并更新对应哈希，保留初次实测失败证据；更新前后完整草稿及 pending 摘要、项目、资料、任务一致性检查。 |

`打开财经月结亲测.lnk` 已更新到 r10，原工作空间及私有模型配置沿用，未放入交付包。

## 【实现说明】

U3 根因是附件显示与资料预览没有连接：上传、任务读取和原件存储已经存在，但消息中的文件卡片只是无动作的文本。修补直接复用已有预览器，不增加另一套独立页面。

U4 是包内真实样例验收发现的第二个缺陷。财经工作簿由流式写入器生成，没有可选的 Excel dimension 元数据；openpyxl 只读模式的 `max_row`、`max_column` 因此为 None，旧代码按 0 处理。初次包体检查虽然验证了页签和下载，却未断言实际单元格非空，不能证明可读。该次记录已明确改为 **ok=false**，保留为 `verification/source-preview-package-v07-attempt1.json`。本轮强化断言后重跑，没有用页签列表代替内容验收。

关键代码摘录：

```python
metadata = inspect_workbook(path)
names = [item['name'] for item in metadata['sheets']]
selected = sheet or names[0]
if selected not in names:
    raise ArtifactError('指定页签不存在')
selected_metadata = next(item for item in metadata['sheets'] if item['name'] == selected)
# Bounded XML inspection derives actual cells, even if dimension is absent/stale.
total = selected_metadata['max_row']
width = selected_metadata['max_column']
if width > 200 or total > MAX_ROWS:
    raise ArtifactError('Excel原始网格超过完整预览预算，请下载原件或明确数据区域')
```

页签内容仍按原始单元格坐标、合并锚点、公式原文及精确 XML 数值展示；不推断表头，不执行公式，不把内容预览声称为原版式还原。历史消息的附件版本用于精确解析，没有静默切换最新版。

自测证据：

- `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_workspace_artifacts.py -q`：**30 passed in 17.47s**，`verification/upload-source-artifact-regression-v03.xml` / `.log`。
- `tests/ui/package.json` 全套 **20 个脚本通过**，包含此前的继续按钮、模型、附件、登录切换及工作区回归；`verification/source-preview-all-ui-v07.log`。新 DOM 点击及预览专项分别见 `source-buttons-v07.log`、`source-preview-ui-regression.log`。
- r10 基础构建校验 15238 文件，运行库 15006 文件／743300599 字节；`verification/desktop-build-v10.log`。真实样例暴露空表后，未交付候选仅替换已测试的首方预览模块并更新对应清单，五处源码、包内文件、`FILES.sha256` 一致；`verification/r10-finalization.json`。运行库、依赖和旧 r9 交付目录均未修改。
- 最终包随包 Python、空继承 PATH、全新中文目录、local_only 实跑：上传原始两文件，逐页签预览、文本核对、下载字节与原件一致、无鉴权拒绝、跨项目拒绝，**ok=true、模型调用 0**；`verification/source-preview-package-v07.json`。物理网络未隔离，不冒称断网实测。
- 包内真实财经工作簿显示：收入底稿 **21 行 × 9 列**、成本底稿 **15 × 9**、预算 **7 × 5**、省份映射 **10 × 4**，四页签均有实际内容。口径文本完整匹配，下载保持原字节。
- r10 启动现有亲测项目后，两份原资料均能通过新入口解析，四页签可读，下载摘要等于原消息冻结摘要；`verification/live-source-preview-v07.json`。
- 更新前活跃任务 0；更新后项目、原任务、附件、草稿及完整 pending 摘要一致，`saved_state_preserved=true`；`verification/trial-r10-before.json`、`trial-r10-after.json`。没有重新上传原资料或重新提交月结。
- 原生 Windows 可访问性树确认实际窗口已显示 `2026-09_业务源表.xlsx` 预览、收入底稿页签、只读说明及“下载原文件”；`verification/source-preview-native-v07.json`。本轮只读取窗口状态，未声称自动鼠标点击或截图视觉验收。

## 【反驳意见】

无反驳。资料上传成功就应能核对原件；来源只读预览与加工结果、财务口径批准分别展示。没有为查看附件引入在线 Office 服务、编辑器或新增依赖。

## 【自评扫描】

1. 缺文件、摘要篡改、跨项目、失效版本、归档、空表、错误/缺失维度、超行列预算、并发与迟到响应已验。文件名按文本转义，公式只显示原文。
2. 最终包空 PATH、新目录、随包运行时、local_only、无模型配置可查看和下载；不要求业务机 Node 构建或安装依赖。
3. 原件不可用返回可操作错误；不支持的格式保留下载路径，不伪装成可原样渲染。单元格预览与原文件下载分开标注。
4. 没有新增 AI 调用、后台重复发送、源文件修改、通用编辑器或独立导航入口。尚未上传的本机文件不冒充已保存的来源快照。
5. 本轮资料查看回归与最终包复验通过。完整财经月结 G1 的上下文容量问题及 G2–G10 未验证状态不因本轮 UI/预览修补改变，不能据此宣布整个财经产品已验收完成。

## 【给豆包的下一步请求】

依用户指令，本轮不调豆包；r10 已打开原项目，用户可直接点击对话中的资料名称亲测。
