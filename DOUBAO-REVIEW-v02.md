# DOUBAO 复审报告 · v02（第一轮独立复审：Atlas DataBuddy H1/H2 最小范围）

> 评审角色：最终验收人 / 严苛 QA（只读，未操作 Codex 窗口/剪贴板，未改代码，未读 llm.json/.env/用户数据）
> 版本：v02 · 2026-10-24
> 审查范围：US-A1/B1/C1 离线闭环、启动无外网探测、错误密钥不阻断、源码 P0 扫描、证据真实性
> 分级：P0（必修）/ P1（应修，功能缺口明确不放行）/ P2（观察）
> 前置原则（用户校准 §8）：功能特性优先于底层完善；能力不达标不放行，不因底层漂亮放行。

---

## 一、结论摘要

**状态：🔧 需要继续迭代（第一轮 H1/H2 最小范围的核心已实现且证据真实，无 P0；但存在 2 项明确功能缺口 + 1 项待补交验收，未达收敛）**
**置信度：高**（所有关键证据已直接核验源码与产物，非二手转述）

本轮最大价值：**US-A1/B1/C1 的离线操作闭环已被真实、可复现的证据证明**——`backend-verification.json` 由仓库内 `verify_backend.py` harness 实跑生成（随包 Python、空 PATH、local_only），完整走通 上传→画像→确定性加工→候选→人工发布→结果→导出→审计，金额精确 `Decimal('118.15')`；bad_model（不可达 base_url + 无效 key）仍完成全链，证明**错误密钥不阻断本地**；启动只探本地 `/api/status`+`/ready`，**无外网探测**。DV-1 已修复。

**无 P0 源码问题。**

---

## 二、已确认项（证据核验，全部通过）

| 项 | 证据位置 | 判定 |
|---|---|---|
| 零配置：空继承 PATH + 随包隔离 Python | `verify_backend.py:54-61`（env PATH=''）；`portable_runtime.py:62-84`（最小 env 白名单、重造 PATH=随包 python/git） | ✅ |
| 启动无外网探测 | `portable_runtime.py:93-134`（只探本地 `/api/status` + `/ready`）；`backend-verification.json:51` `startup_does_not_require_reachable_model=true` | ✅ |
| 错误密钥/缺模型不阻断本地 | `verify_backend.py:303-311` bad_model 全链通过；`portable_runtime.py:104-107`（模型配置为 operator 提供，启动不需密钥） | ✅ |
| US-B1 金额精确 | `verify_backend.py:197-228`：`m_net=c_3−c_4`、filter `paid`，`Decimal==118.15`、导出 CSV 求和 `==118.15`；`backend-verification.json:13/32` | ✅ |
| US-C1 发布前核对 | `verify_backend.py:214-218`：未发布 GET result=409、preview records==2、publish 后 result total==2；`manual_publish_required=true` | ✅ |
| US-A1 上传即用（离线） | `verify_backend.py:124-134/188-196`：multipart 上传→画像→ready；`local-data.html/js` GUI 完整入口 | ✅ |
| 重启保留数据/凭据/端口 | `verify_backend.py:292-299`：重启后 url/token/data 一致、audit ok | ✅ |
| 审计与边界（401/空/超长/空文件） | `verify_backend.py:179-187`：401×2、空名、超长名、空文件、超长消息、空消息全部断言 | ✅ |
| DV-1 修复（OPENCODE_RUNTIME） | `portable_runtime.py:78` `ATLAS_OPENCODE_RUNTIME='1'` | ✅ 已修复 |
| 证据真实性（非手写） | `verify_backend.py:266-336`：异常写 diagnostic log、`ok` 由 harness 判定、token 从不出现在 evidence（`:158-159` redact） | ✅ |
| 视觉规范 R5 | `ui/src/main.jsx:19` + `graph.mjs:3-11`：白底、`#111` 黑线箭头、nodesDraggable/connectable/reconnectable 全 false（只读） | ✅ |
| 技术栈 R2/R8 | `desktop/main.cjs`（Electron 壳）、`ui/`（React+Vite+ReactFlow 溯源，`ui/dist` 构建产物随包）、langgraph/MCP 保留 | ✅ |
| 哈希/许可证据 | `vendor/metadata/runtime-manifest.json`（Python/OpenCode/Git/170 包 lock_sha256，15009 处 sha256）；`dist/FILES.sha256` | ✅ |
| 权限边界（新增 API） | `api.py:542/834/472/372` 等全部 `Depends(auth)`；`local_processing` → `skip_model_planning=True`（:591） | ✅ 无 P0 |

---

## 三、问题清单

### P1 · 功能缺口（明确不放行）

**FG-1 [P1] US-D1「真实模型三件套」未验证（能力缺口）**
- **位置**：`verification/windows-offline-06/backend-verification.json:6` `"real_ai_evaluated": false`；`verify_backend.py` 全链 `no AI calls`
- **现象**：现有证据只证明**确定性本地加工**闭环（规则驱动、无 AI），未验证 US-D1 的"用户描述目标→Agent 查口径→生成方案→产出 Excel/Word/PPT 三件套"。
- **为什么是问题**：违反用户校准 §8（功能特性优先）；US-D1 是产品核心价值（对话驱动产出三件套），当前没有任何真实模型证据。
- **期望行为**：用**真实 DeepSeek** 完成一次"按办事处比较净收入与预算、退款扣除、输出 Excel/Word/PPT"并核对产物；或至少在 P1 未验状态下**不放行** US-D1。
- **修复建议**：补 `acceptance/` 下 real-model harness（复用 verify_backend.py 框架，替换为真实模型 config），记录三件套文件哈希+内容抽样核对。

**FG-2 [P1] US-E1「本地文件夹模式」Windows 完整适配未验证（能力缺口）**
- **位置**：`DOUBAO-ACCEPTANCE-v0.md` US-E1；Codex 本轮明确"尚未声称通过"
- **现象**：原 `dir_fd` 本地文件夹打开模式在 Windows 上的完整适配（原件不覆盖、产物独立子目录、仅监听本机）未声称通过。
- **为什么是问题**：US-E1 是离线/零配置的核心用户场景（文件留在自己电脑），缺口不标出会误导验收。
- **期望行为**：Windows 上选择本地文件夹→启动→原件不被覆盖、产物在独立子目录、仅 127.0.0.1。
- **修复建议**：补 Windows 文件夹模式验收脚本（创建源文件→打开文件夹→加工→断言原件哈希不变、产物在子目录、无外部监听）。

### P2 · 待补交 / 观察

**OB-1 [P2] dist 包内验收结果待补交**
- **位置**：`dist/Atlas-DataBuddy-win32-x64/`
- **现象**：当前真实证据在包外（`verification/windows-offline-06`）；dist 包内 `resources/app` 结构完整（已验证 runtime/python、portable_runtime、ui/dist、local-data 均在内），但**在 dist 包本体上跑的验收结果**尚未补交。
- **为什么是问题**：验收的最终对象是交付包，不是源码树；包内打包/路径是否与源码树一致需在包内复验。
- **期望行为**：对 `dist/Atlas-DataBuddy-win32-x64` 本体跑一遍 verify_backend（`--app-root dist/...`），产出包内 `backend-verification.json`。
- **修复建议**：`python acceptance/offline/verify_backend.py --app-root "<dist>" --evidence-dir verification/windows-offline-dist-01`。

**OB-2 [P2] 离线验证基于 local_only 策略，未在真实断网/防火墙隔离 VM 复验**
- **位置**：`backend-verification.json:7` `os_firewall_or_disconnected_vm_evaluated=false`
- **现象**：离线通过"应用层 `ATLAS_NETWORK_MODE=local_only` + telemetry 全关"达成，未在物理断网/防火墙隔离环境确认无隐蔽外连。
- **为什么是问题**：H2a 判定"离线启动/查看/本地加工导出必须可用"，当前缺 OS 层断网证据。
- **期望行为**：在断网（或防火墙全阻断）环境跑同一 harness，确认全链仍通过。
- **修复建议**：Windows 防火墙出站全禁（或断网）再跑 verify_backend；不通过则该 P2 升 P1。

**OB-3 [P2] `python-packages.json` 无哈希字段**
- **位置**：`vendor/metadata/python-packages.json`（684 行，仅 name+version）
- **现象**：包清单无 sha256；哈希在 `runtime-manifest.json`（15009 处）中。
- **为什么是问题**：清单与哈希分离，对账需跨文件；非安全缺陷。
- **期望行为**：包清单可直接定位每包哈希。
- **修复建议**：在 python-packages.json 每项补 `sha256`（从 runtime-manifest 回填），便于独立核验。

---

## 四、5 维强制扫描

1. **功能完整性**：US-A1/B1/C1 离线闭环已证实；US-D1/US-E1 为明确功能缺口（FG-1/FG-2，P1 不放行）；其余 US 属既有能力，待全量回归。**存在缺口**（已标出）。
2. **边界与异常**：401/空/超长/空文件/重启/错误密钥/缺 Git 已覆盖；缺真实断网 OS 层验证（OB-2）。**未发现**未声明边界被放行。
3. **代码质量**：portable_runtime.py / main.cjs / security.cjs / local-data.js / verify_backend.py 均无错误吞没、无魔法数字、命名清晰。**未发现** P0 级质量问题。
4. **离线/零配置**：空 PATH+随包 Python+本地 readiness 已证实；错误密钥不阻断已证实。**未发现**启动依赖外网路径。
5. **反向挑战**：本轮无 Codex 反驳需回应；DV-3/DV-4 维持撤回（不改无缺陷代码），DV-1 已确认修复。**无新增撤回**。

---

## 五、给 Codex 的下一步指令

补交三件，按优先级：① 在 `dist` 交付包本体上跑 verify_backend 产出包内 `backend-verification.json`（OB-1）；② 补真实模型三件套验收 harness 或明确 US-D1 维持不放行（FG-1）；③ 补 Windows 文件夹模式（US-E1）验收脚本（FG-2）；④ 条件允许时在断网/防火墙隔离环境复跑一次（OB-2）。交付时逐项回传证据路径与我核对。

---

## 六、撤回记录

- DV-3（撤回维持）：`admit_model_request` 记账无上限——接受 Codex 反驳，维持撤回，本轮未复提。
- DV-4（撤回维持）：`workspace_gateway` semaphore 非原子——接受 asyncio 语义 + 实跑证据，维持撤回，本轮未复提。
- DV-1（已确认修复）：`run-local.sh` 未设 `ATLAS_OPENCODE_RUNTIME`——`portable_runtime.py:78` 已显式 `'1'`，本轮关闭。
