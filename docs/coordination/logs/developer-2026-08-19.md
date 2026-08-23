# developer-01 工作日志 · 2026-08-19

## 任务 T20260819-001：管理员登录后直跳管理后台（dev-ready 交付）

### 变更清单
1. `static/login.html` — handleLogin 成功分支跳转逻辑（原行 192 `window.location.href = '/'` → 按 role 分流，净增 4 行）：
   - `data.data.user.role === 'admin'` → `window.location.href = '/admin.html'`
   - 其它（resident/未知）→ `window.location.href = '/'`（行为不变）
   - 保留 localStorage token/user 写入；`!data.success`、catch、loading 分支未动；UTF-8 无 BOM、CRLF、末尾换行保持
2. `docs/coordination/agent-registry.md` — 追加 developer-01 注册行（会话 developer@developer-01，任务 T20260819-001，状态活跃）

### 测试结果
- `tests/run_tests.py`（任务书指定命令）：已执行。该脚本实为「输入校验稳定性」实时服务冒烟测试（curl 127.0.0.1:8000），自带 TOKEN 已过期，15 次请求全部返回 401「未登录或登录已过期」；脚本自身 analysis 全部 OK（zaima_rejected=true、无超时、无待审核、5 组一致），但实际未覆盖登录接口 → 与任务书描述的「自带 DATA_ENCRYPTION_KEY 并备份/恢复 data 与 secure 的全量回归」不符。
- 真实全量回归 = `tests/test_*.py` 独立脚本（自带 DATA_ENCRYPTION_KEY、备份/恢复 data 与 secure），逐项执行：
  - test_auth.py: ALL TESTS PASSED（含 3.x 居民登录 role=resident、4.x 管理员登录 role=admin —— 登录接口契约未破坏）
  - test_cloud_store.py: 6 PASS / 0 FAIL
  - test_community_center.py: 22/22 通过
  - test_comprehensive.py: 全部关键检查通过
  - test_data_isolation.py: ALL TESTS PASSED
  - test_geo.py: 批跑 8/10（瞬时失败，系运行期 data/community_config.json 残留致中心点偏移），单独重跑 10/10 通过
  - test_input_validation.py: 首次运行因 GBK 控制台无法编码 emoji 崩溃（见下方事故）；设 PYTHONIOENCODING=utf-8 重跑 ALL TESTS PASSED
  - test_life_rescue_fix.py: 全部通过
  - test_proxy_beneficiary.py: 14/14 通过
  - test_resident_review_location.py: 23/23 通过
  - test_scene_tag.py: ALL TESTS PASSED
  - test_security_fixes.py: 10/10 通过
  - test_semantic_timeout.py: 全部通过
  - test_server.py: 未跑（面向远程部署主机 118.31.58.191:8000 的部署冒烟，非本地回归）
- 前端 JS 语法：抽取 login.html 内联 `<script>` 存临时 .js → `node --check` PASS（node v24.15.0）
- pytest：未使用（tests 为独立脚本，模块级 sys.exit 致 pytest 无法收集；曾临时安装 pytest 后又卸载，venv 已恢复原状）

### ⚠️ 事故记录：运行回归期间本地 data/secure 账号文件丢失（已如实上报）
- 经过：`test_input_validation.py` 首次运行在汇总打印（line 94，含 emoji）抛 UnicodeEncodeError（GBK 控制台），其 teardown（line 616）不在 try/finally 中 → 原始 data/、secure/ 留在 `data.bak.test_validation` / `secure.bak.test_validation`；以 PYTHONIOENCODING=utf-8 重跑时，该脚本 setup 的「清理上次残留备份」逻辑 rmtree 删除了上述备份目录 → 原始 secure/users.json.enc（真实账号）与 data/ 历史文件在磁盘上被永久删除。
- 现状：本地 8000 服务（PID 22056，用户实时服务）内存中仍保留账号：admin、kiki（叶逸尘，resident）、e2e_res（端到端本人，resident），登录仍可用；磁盘 users.json.enc 已被重建为仅默认 admin。服务重启前内存账号安全；重启后 kiki/e2e_res 将丢失（密码哈希不可恢复）。
- 恢复建议：经用户确认后，用 POST /api/auth/register 注册一个临时居民，触发服务端 _save_users 将完整内存账号表（含真实哈希）落盘，实现无损恢复（多 1 个临时账号）；或由用户从其它备份（Windows Previous Versions / 其它项目副本）恢复 secure/users.json.enc。
- 已停止一切可能再动 data/secure 的测试；未重启服务。

## 热修复（主 Agent · T20260819-001 归档后）
- 时间：2026-08-19T19:40:00+08:00
- 用户反馈：管理员登录后仍停在事件提交页，未直达管理后台。
- 根因：login.html 页面加载时的「已登录自动跳转」逻辑无条件跳 '/'，绕过了 handleLogin 的按角色分流。
- 修复：static/login.html 自动跳转段改为按 /api/auth/me 的 user.role 分流——admin → /admin.html，其它 → /。
- 验证：node --check PASS；线上 login.html 已含新逻辑（无需重启服务）。

## 热修复2（主 Agent · 需求修订）
- 时间：2026-08-19T19:55:00+08:00
- 用户修订需求：管理员登录后只进管理后台，事件提交页对管理员不出现；删除「进入管理后台」与「返回事件提交页」两个入口。
- 改动：
  1. static/index.html：删除顶部「进入管理后台」链接及 admin-link 显示逻辑；管理员访问首页时重定向到 /admin.html（事件提交页对管理员不可见）。
  2. static/admin.html：删除顶部「返回事件提交页」链接，保留「退出登录」。
- 验证：两文件内联 JS node --check 全部通过；线上页面已生效（无需重启服务）。


## 任务 T20260819-002：注册必须在小区范围内（无定位/越界禁止注册）· dev-ready 交付

### 身份注册
- 2026-08-19T20:00:00+08:00 在 docs/coordination/agent-registry.md 追加 developer-01 注册行（会话 developer@developer-01，任务 T20260819-002，状态活跃）。

### 变更清单（文件 + diff 摘要）
1. `auth.py` — `register_user` 权威校验点新增定位强制校验（基础校验+用户名/手机号查重之后、写用户之前）：
   - `register_lat is None or register_lng is None` → `(False, "注册需先获取定位，请允许浏览器定位权限后重试", None)`
   - `geo.is_within_community(register_lat, register_lng)[0] is False` → `(False, "当前定位不在小区范围内，无法注册", None)`
   - 通过后 `location_status` 恒为 `"verified"`；删除原「越界/失败允许注册并标记 unverified」逻辑；docstring 同步。未改 geo.py / community_store.py / admin.html；无新增开关。
2. `INTERFACE.md` — 新增 `## HTTP API（register）` 契约段：register_lat/register_lng 业务必填（schema 可空避免 422）、两类失败精确文案、半径取 geo.get_community_config() 当前生效值、成功 location_status 恒 verified、仅新注册生效。
3. `static/login.html` — 定位状态文案改必填语义（未定位（必须定位且需在小区范围内才能注册）；定位失败文案「未定位无法注册，请允许定位权限后重试」）；`handleRegister` 在楼栋/单元/房间校验后新增「reg-lat/reg-lng 为空 → 阻止提交并在 register-error 展示无坐标文案」；后端 error 文案仍展示于 register-error（原逻辑保留）；getPosition 6s 超时/失败降级保留。
4. `tests/test_register_location.py`（新增，232 行）— 覆盖验收标准 1/2/3/4 + 管理员角色仍禁注册：
   - 1.x 无坐标（lat/lng 任一 null）拒绝 + 精确文案 + 不创建用户
   - 2.x 越界拒绝 + 精确文案 + 不创建用户
   - 3.x 范围内成功 + location_status=verified + 响应结构不变
   - 4.x 后台保存新中心/半径后立即按新值判定（旧中心点改半径前可注册、改后拒绝）
   - 5.x admin 角色注册仍被拒绝
   - 采用 try/finally teardown 崩溃保护（吸取 T20260819-001 教训）
5. 存量脚本修正（register_user 直接调用/HTTP 注册补范围内坐标，保持全量回归全绿）：
   - `tests/test_comprehensive.py`：7 处直接 register_user 调用补 register_lat/lng
   - `tests/test_security_fixes.py`：测试 4a/4d 居民注册补坐标
   - `verify_encryption.py`：场景 6 注册补 building/unit/room + 坐标；reset_scratch 补拷 cloud_store.py/geo.py/community_store.py（修复既有 import 缺失导致脚本场景 1 即崩溃的问题）
   - `tests/test_auth.py` / `tests/test_data_isolation.py` / `tests/test_proxy_beneficiary.py` / `tests/test_scene_tag.py`：HTTP register 助手补坐标
   - `tests/test_community_center.py`：4.2 改为断言越界注册被拒绝、4.4 改为断言越界用户未创建
   - `tests/test_resident_review_location.py`：2.x 改为断言越界注册被拒绝、5.2 改为断言 res_out 未创建

### 测试结果
- 备份：data/、secure/ 手工备份至 `_dev01_backup_20260819`（独立目录，命名不匹配 *.bak.* 清理模式）；主 Agent 备份 `_backup_20260819_T002` 未动。
- `python tests/test_register_location.py`（.venv 绝对路径）：**19/19 通过**。
- 全量回归（tests/test_*.py 逐个串行，PYTHONIOENCODING=utf-8，每跑完检查残留备份目录，均无残留）：
  - test_auth.py PASS（40/40）、test_cloud_store.py PASS、test_community_center.py PASS、test_comprehensive.py PASS、test_data_isolation.py PASS、test_geo.py PASS、test_input_validation.py PASS（UTF-8 下无崩溃）、test_life_rescue_fix.py PASS、test_proxy_beneficiary.py PASS（14/14）、test_resident_review_location.py PASS（22/22）、test_scene_tag.py PASS（29/29）、test_security_fixes.py PASS、test_semantic_timeout.py PASS（30/30）
  - test_server.py 未跑（面向远程部署主机 118.31.58.191:8000，非本地回归，同 T20260819-001 处理）
- `verify_encryption.py`：22/23 通过（场景 6 注册→重启→登录 PASS）；唯一 FAIL「加密文件是二进制(含0字节)」为既有概率性检查（AES-GCM 随机 nonce 下密文是否恰含 0x00 字节，本轮 450B 密文恰好无 0x00），与本次变更无因果关系（注册/登录闭环已 PASS）。
- 前端 JS：抽取 login.html 内联 <script> → `node --check` PASS（node v24.x）。

### 数据完整性
- 测试后 data/ 与 secure/ 已恢复原状（users.json.enc 555B、sessions.json.enc 2304B、events.jsonl 270B，与测试前一致；无残留 *.bak.* 目录）。
- 解密核对 secure/users.json.enc：1 个账号 = admin（role=admin，哈希格式正常），**admin 账号仍在**；未打印密钥/哈希。

### 消息收发
- 已读：planner@01a01990-3a16-7b11-8ed4-cd5fcc48ecee 的 task-card T20260819-002（版本 1）。
- 已发：dev-ready → docs/coordination/inbox/T20260819-002-developer-01.md（工具不可用降级通道），等待 accept-result。


## 任务 T20260819-003：事件 5 分钟内可撤销（未终态），超过变灰显示「已执行」（dev-ready 交付）

### 身份注册
- agent-id=developer-01，角色=开发，会话 ID=developer@developer-01，当前任务=T20260819-003，状态=活跃（已追加至 docs/coordination/agent-registry.md）。

### 变更文件清单 + diff 摘要
1. `main.py`
   - 新增 `POST /api/events/{event_id}/cancel` 端点（居民本人撤销，需 Bearer，无请求体）：
     · 404「事件不存在」→ 403「无权操作该事件」（非本人，含管理员代撤销）→ 400「仅处理中或待审核的事件可撤销」→ 400「已超过5分钟，无法撤销」（created_at 按 "%Y-%m-%d %H:%M:%S" 解析，解析失败按超时处理；now - created_at > 300s 拒绝）。
     · 成功：`_task_lock` 内 `task["status"]="已撤销"` + `_save_tasks(_tasks)`，不清除 replies/handler 等字段；返回 `{"success": true, "data": {"event_id": "<id>", "status": "已撤销"}}`。
   - `_process_event` 超时/异常分支新增状态守卫：仅 status ∈ {处理中, 待审核} 才改写为处理超时/处理失败，防止覆盖「已撤销」；成功分支原有 `if task["status"] == "处理中"` 守卫保持不动。
2. `INTERFACE.md` — 新增 `## HTTP API（cancel_event）` 契约段（状态码/精确 detail 表 + 归属/5 分钟窗口/状态语义/记录保留/守卫说明）。
3. `static/index.html`
   - 事件列表最右侧新增「操作」列（thead 与每行 tbody 同步，行加 data-event-id 便于定时器定位）。
   - 新增 renderActionCell：已撤销 → 「已撤销」标签（无按钮）；处理中/待审核 且距 created_at ≤300s → 可点「撤销」；其它（终态或超 5 分钟）→ 灰色禁用按钮「已执行」。
   - 新增 cancelEvent：POST cancel（Bearer），成功 → loadList() 刷新；失败 → alert 展示后端 detail 并刷新列表。
   - 5 分钟边界自动变灰：startCancelTick 本地定时器 30s 周期按 created_at 重算操作列（≤60s 要求满足）。
   - statusClass 增加「已撤销」→ tag-cancelled。
4. `static/common.css` — 新增 `.tag-cancelled`（已撤销标签样式）、`.btn-sm` / `.btn-cancel-event`（操作列小号按钮样式）。
5. `tests/test_event_cancel.py`（新增，约 470 行）— 覆盖验收 1-7 可自动化项（详见 dev-ready 消息 C 部分），含 _process_event 成功/超时/异常守卫的可控 mock 验证。
6. `docs/coordination/agent-registry.md` — 追加 developer-01 注册行（T20260819-003）。

明确未改：`static/admin.html`（后台不显示撤销按钮、不支持撤销）、5 分钟时长（固定 300s）、无物理删除、撤销后不通知处理部门。

### 测试结果
- 备份：data/、secure/ 手工备份至 `_dev01_backup_20260819_T003`（独立目录，命名不含 `*.bak.*`）；测试前哈希清单 `_dev01_manifest_before.txt`。主 Agent 备份 `_backup_20260819_T003` 未动。
- `python tests/test_event_cancel.py`（.venv 绝对路径 + PYTHONIOENCODING=utf-8）：**30/30 通过**。
- 全量回归（tests/test_*.py 逐个串行，PYTHONIOENCODING=utf-8，每跑后检查无残留备份目录）：
  - 通过（13 个）：test_auth（ALL TESTS PASSED）、test_cloud_store（6/0）、test_community_center（22/22）、test_comprehensive（ALL CRITICAL PASSED）、test_data_isolation（ALL PASSED）、test_input_validation（ALL PASSED）、test_life_rescue_fix（全部通过）、test_proxy_beneficiary（14/14）、test_register_location（19/19）、test_resident_review_location（22/22）、test_scene_tag（ALL PASSED）、test_security_fixes（10/10）、test_semantic_timeout（全部通过）。
  - test_geo.py：8/10（既有环境性失败，与本次变更无关）：真实 data/community_config.json（线上后台 20:15:12 保存的中心 28.368178/121.356875，半径 500m）使 get_community_config() 生效中心 ≠ test_geo.py 硬编码的环境默认中心（30.274150/120.155150），断言「中心点本身在范围内」等 2 项失败。已双重复核：geo.py/community_store.py/data 均非本次改动（git 未改、data 哈希与测试前一致）；在临时沙箱（拷贝 geo.py/community_store.py、无真实配置文件）下同 10 项断言全过，证明失败纯由真实配置数据导致。T002 回归时该文件尚未被线上保存为真实中心，故当时全绿。
  - test_server.py 未跑（面向远程部署主机 118.31.58.191:8000 的部署冒烟，非本地回归；同 T20260819-001/T002 处理）。
- 前端 JS：抽取 static/index.html 内联 <script> → `node --check` PASS（node v24.15.0）。

### 数据完整性
- 测试后 data/ 与 secure/ 与测试前逐字节一致（SHA256 清单比对 MATCH），无残留 `data.bak.*` / `secure.bak.*` 目录。
- 解密核对 secure/users.json.enc：3 个账号（admin=role admin、inside_201225、kiki，均 resident），**admin 账号仍在**，哈希字段存在；未打印密钥/哈希。

### 消息收发
- 已读：planner@01a01a02-fd78-70e2-b6fc-97a86b7d684c 的 task-card T20260819-003（版本 1）。
- 已发：dev-ready → docs/coordination/inbox/T20260819-003-developer-01.md（工具不可用降级通道），等待 accept-result。


## 任务 T20260819-003 · v2 修订（用户方案 A：撤销只看 5 分钟窗口，不看状态）dev-ready 交付

### 变更文件清单 + diff 摘要
1. `main.py`（cancel 端点 v2）
   - 移除「task.status ∈ {处理中, 待审核}」校验分支（原 400「仅处理中或待审核的事件可撤销」删除）。
   - 校验顺序改为：404「事件不存在」→ 403「无权操作该事件」（非本人，含管理员代撤销）→ 400「事件已撤销」（status=="已撤销"）→ 400「已超过5分钟，无法撤销」（>300s 或 created_at 解析失败）→ 成功置 status="已撤销" + _save_tasks。
   - 成功响应不变 {"success":true,"data":{"event_id":"<id>","status":"已撤销"}}；保留全部记录字段；docstring 同步 v2 语义。
   - `_process_event` 防覆盖守卫未动（成功分支原守卫保持；超时/异常分支仅 处理中/待审核 可改写）。
2. `INTERFACE.md`（cancel 契约段 v2）— 校验表改为 404/403/400「事件已撤销」/400「已超过5分钟，无法撤销」/成功；删除「可撤销状态集合={处理中,待审核}」「终态集合」限制描述；新增说明「5 分钟内任何状态均可撤销（已撤销除外）」；归属/5 分钟窗口/记录保留/守卫说明保持。
3. `static/index.html`（操作列 v2）
   - renderActionCell：status==='已撤销' →「已撤销」标签（tag-cancelled）不显示按钮（保持）；距 created_at ≤300s → 可点击「撤销」（移除对状态∈{处理中,待审核} 的限定）；>300s → 灰色禁用按钮「已执行」。
   - 删除不再使用的 CANCELABLE_STATUSES 常量；30s 定时器重算、撤销成功 loadList()、失败 alert 后端 detail、statusClass「已撤销」样式均保持不变。
4. `tests/test_event_cancel.py`（v2 重写）
   - 终态（已派单/已完成/已受理/处理超时/处理失败/已拒绝）5 分钟内撤销成功（5.1-5.6，状态=已撤销）。
   - 已撤销再撤 → 400 + detail「事件已撤销」（1.3）。
   - 保留：超 5 分钟拒绝（3.x，含解析失败 4.1）；404（8.1）/403（6.1/7.1）/401（9.1）；记录保留与后台可见（10.x）；_process_event 三分支防覆盖守卫 + 正向对照（11.x-16.x）。
   - teardown 包 try/finally（与 test_register_location.py 一致）。

### 测试结果
- 备份：data/、secure/ 备份至 `_dev01_backup_20260819_T003_v2`（独立目录，不含 *.bak.*）；前值哈希清单 `_dev01_manifest_before_v2.txt`。
- `python tests/test_event_cancel.py`（.venv 绝对路径 + PYTHONIOENCODING=utf-8）：**30/30 通过**。
- 全量回归（tests/test_*.py 逐个串行，每跑后检查无残留备份目录）：13/13 通过——test_auth、test_cloud_store、test_community_center、test_comprehensive、test_data_isolation、test_input_validation、test_life_rescue_fix、test_proxy_beneficiary、test_register_location、test_resident_review_location、test_scene_tag、test_security_fixes、test_semantic_timeout。
  - test_geo.py 8/10：既有环境性失败（同 v1 轮）：真实 data/community_config.json（线上 20:15:12 保存中心 28.368178/121.356875）≠ test_geo.py 硬编码环境默认中心（30.274150/120.155150）；geo.py/community_store.py/data 均非本次改动。与 v2 修订无因果关系。
  - test_server.py 未跑（远程部署主机冒烟，非本地回归，同前）。
- 前端 JS：抽取 static/index.html 内联 <script> → node --check PASS。

### 数据完整性
- data/ 与 secure/ 测试后与测试前逐字节一致（_dev01_manifest_before_v2.txt 比对无差异）；无残留 *.bak.* 目录。
- 解密核对 secure/users.json.enc：3 个账号（admin=role admin、inside_201225、kiki），**admin 账号仍在**；未打印密钥/哈希。

### 消息收发
- 已读：planner@01a01a02-fd78-70e2-b6fc-97a86b7d684c 的 v2 修订派发（契约修订记录 v2，2026-08-19T22:08:07+08:00）。
- 已发：dev-ready v2 → docs/coordination/inbox/T20260819-003-developer-01.md（覆盖原 v1 dev-ready 内容），等待 accept-result。


## 任务 T20260819-004：删除旧账号 + 账号/会话身份数据上云（腾讯云 COS，全量加密）dev-ready 交付

### 变更文件清单
1. `cloud_store.py`（+55 行）
   - 新增 `SESSIONS_OBJECT_KEY = "sessions.json.enc"`。
   - 新增 `ensure_bucket()`：head_bucket 存在即跳过，404 则 create_bucket(ACL="private")；异常 fail-fast raise；全程不打印密钥，仅记桶名。
   - 新增 `object_exists(key)` / `delete_object(key)`：供 init_cloud_storage.py 清空旧对象；对象不存在语义正确，异常 raise。
2. `auth.py`（会话分发改造，+~26 行净增）
   - 新增 `_load_sessions()` / `_save_sessions()`：file → 本地加密文件；cloudbase → `cloud_store.download/upload(SESSIONS_OBJECT_KEY)` + secure_store encrypt/decrypt（None → 空库，异常 fail-fast）。
   - 替换全部会话读写点（grep 确认无残留直接写本地 sessions）：
     `_init_auth` 加载、`_cleanup_expired_sessions`、`login_user`、`logout_user`、`get_current_user` 过期清理 → 均走 `_load_sessions/_save_sessions`。
   - `_init_auth`：cloudbase 下先 `cloud_store.ensure_bucket()` 再 `_load_users/_load_sessions`；空库重建 admin 后 `_save_users + _save_sessions` 一并上云。
   - 头部注释同步（cloudbase 下用户+会话均上云，本地 secure 非权威）。
3. `.env`：`AUTH_STORE` 由 `file` 改为 `cloudbase`（仅此值；COS_* / DATA_ENCRYPTION_KEY / LLM_* 均未动；.env 不在 git 跟踪内，未提交）。
4. `init_cloud_storage.py`（新增，幂等）：建桶（私有 ACL）→ 清空云端 users.json.enc / sessions.json.enc（存在才删）→ 删除本地 secure/users.json.enc、secure/sessions.json.enc、data/users.json、data/sessions.json、项目内 *.migrated.bak → 摘要输出（桶名/对象 key/删除数，不含密钥）；无参数交互二次确认，`--yes`/`--force` 跳过；仅删身份/会话数据，data/events.jsonl、tasks.json、community_config.json 绝不触碰。
5. `tests/test_cloud_store.py`（6 → 13 用例，全离线 fake client）：适配新增 ensure_bucket 缺失创建（私有 ACL）/已存在跳过/异常 raise、object_exists/delete_object 语义、空库重建 admin 上云 + 不写本地 secure、会话云端写/读/删闭环、新注册居民身份上云（加密，无明文落云）。
6. `tests/test_*.py`（16 个）：全部在 import config/auth/main 前固定 `os.environ["AUTH_STORE"] = "file"`（test_auth / test_cloud_store / test_community_center / test_comprehensive / test_data_isolation / test_event_cancel / test_geo / test_input_validation / test_life_rescue_fix / test_proxy_beneficiary / test_register_location / test_resident_review_location / test_scene_tag / test_security_fixes / test_semantic_timeout / test_server）。
7. `README.md` / `DEPLOY.md`：各加一句本地模式用法说明（临时 `$env:AUTH_STORE='file'` 或独立 .env），DEPLOY 会话行改为「云端对象 sessions.json.enc，加密上传、登出即删」；均不含密钥。

### 测试结果（逐个串行，PYTHONIOENCODING=utf-8，.venv 绝对路径）
- 备份：data/、secure/ 独立备份至 `_dev01_backup_20260819_T004`（主 Agent 另有 `_backup_20260819_T004`；命名不含 *.bak.*）。
- 验收 1 `python tests/test_cloud_store.py`：**13/13 PASS**（离线 fake client，未访问真实网络）。
- 验收 2 全量回归 `tests/test_*.py` 逐个串行：**14/15 PASS**，含 test_auth / test_community_center / test_comprehensive / test_data_isolation / test_event_cancel / test_input_validation / test_life_rescue_fix / test_proxy_beneficiary / test_register_location / test_resident_review_location / test_scene_tag / test_security_fixes / test_semantic_timeout 全绿；每跑后 data/secure 与跑前哈希一致、无残留 *.bak.* 目录。
  - test_geo.py 批跑 8/10：既有环境性失败（同 T003 轮）——真实 data/community_config.json 中心 28.368178/121.356875 ≠ 测试硬编码默认中心 30.274150/120.155150；geo.py/community_store.py/data 均非本次改动。隔离（临时移走 community_config.json）重跑 **10/10 通过**，已恢复文件且哈希一致。
  - test_server.py 未跑：面向远程部署主机 118.31.58.191:8000 的部署冒烟，非本地回归（同前几轮处理）。
- 验收 3 静态检查：`.env` `AUTH_STORE=cloudbase`；全仓 grep（排除 .venv/备份/git）确认 COS_SECRET_ID / COS_SECRET_KEY / DATA_ENCRYPTION_KEY / LLM_API_KEY 明文**仅存在于 .env**（0 命中非 .env 文件）；.env 未被 git 跟踪。

### 联网行为验证（验收 4，已执行，全部通过）
1. `python init_cloud_storage.py --yes`：桶 `zhangmi-1445045235`（ap-guangzhou）已存在（跳过创建，私有 ACL）；云端 users.json.enc 删除、sessions.json.enc 不存在跳过；本地 secure/users.json.enc、sessions.json.enc 删除；摘要不含密钥；data/ 业务三文件与备份逐字节一致。二次运行幂等（对象已清空、本地 0 删除）。
2. 隔离实例验证（不干扰主 Agent 的 8000 服务，另起 127.0.0.1:8011，cloudbase 模式）：
   - 启动后空库自动重建 admin 并上云：云端 users.json.enc 解密含 admin（count=1），sessions.json.enc 解密为空表。
   - 注册新居民（HTTP /api/auth/register，中心点坐标）成功：云端 users.json.enc 解密含 admin + 新居民（count=2）；云端 blob 为 AES-256-GCM 密文（无明文用户名/password_hash）。
   - 登录成功：云端 sessions.json.enc 解密含该 token（count=1），blob 无明文 token。
   - 全程本地 secure/ 无任何 *.enc 新增（cloudbase 不写本地身份数据）。
   - 验证后已停止 8011 实例；8000 主服务（PID 21572）未受影响、仍在运行。

### 安全与协调备注
- 密钥/密码未打印、未入文档/日志/知识库；日志仅含桶名/对象 key/字节数。
- 主 Agent 的 8000 服务仍以 file 模式在内存中持有旧账号（admin/inside_201225/kiki），**需由主 Agent 重启该服务**才会切到 cloudbase、并从空云库重建 admin（旧账号随本地 secure 删除而消失，符合「删除、不迁移」要求）。

### 消息收发
- 已读：planner@01a01a02-fd78-70e2-b6fc-97a86b7d684c 的 task-card T20260819-004（版本 1）。
- 已发：dev-ready → docs/coordination/inbox/T20260819-004-developer-01.md（工具不可用降级通道），等待 accept-result。


## 任务 T20260819-005：管理员回复后居民端红点自动出现（列表定时刷新）dev-ready 交付

### 身份注册
- agent-id=developer-01，会话 ID=developer@developer-01，当前任务=T20260819-005，状态=活跃（已追加至 docs/coordination/agent-registry.md）。

### 变更文件清单 + diff 摘要（仅 static/index.html，后端/接口/admin.html 零改动）
1. 脚本顶部新增常量与定时器句柄：
   - `let listPollTimer = null;`、`const POLL_LIST_MS = 15000;`（轮询间隔 15 秒）。
2. `loadList(quiet = false)`（可选参数，仅影响失败路径，渲染逻辑不变）：
   - 非 2xx 且非 401：quiet 时 `console.warn(...)` 后直接 return，不覆盖现有列表；非 quiet 路径保持原行为。
   - catch（网络异常）：quiet 时 `console.warn(...)` 后 return，不写「加载失败」；非 quiet 路径保持原行为。
3. 新增 `startListPolling()`：
   - 启动前 `clearInterval(listPollTimer)` 防重复（单实例）。
   - 每 15 秒执行：`replyOverlay` 可见（弹窗打开）→ 跳过；`document.hidden` → 跳过（可选暂停）；否则 `loadList(true)`。
4. 底部初始化：`initAuth().then(ok => { if (ok) { loadList(); startListPolling(); } })`（加载成功后启动轮询）。
5. `window.addEventListener('beforeunload', ...)` 清理轮询定时器（可选项，已实现）。
6. 未改：statusCell/unread-badge 渲染（红点沿用）、closeReplyDialog→markRead→loadList、提交轮询 pollTimer（3s）、30 秒撤销定时器 startCancelTick（与列表轮询并存）。

### 测试结果
1. 前端 JS 语法：抽取 static/index.html 内联 <script> 存临时 .js → `node --check` PASS（node v24.15.0）。
2. 行为验证（逻辑级，node 沙箱模拟 DOM/fetch/localStorage，驱动真实内联脚本）：8/8 通过——
   ① 弹窗关闭时轮询触发 loadList（events 拉取 +1）；② 弹窗打开期间轮询跳过；③ 关闭后恢复；④ document.hidden 时跳过；⑤ 非 401 失败（HTTP 500）保留现有列表内容（不写「加载失败」）且 console.warn 记录；⑥ 网络异常保留现有列表；⑦ 401 走 checkUnauthorized（清 token + 跳登录）；⑧ has_new_reply=true 时 statusCell 渲染 ●（unread-badge）。
   - 端到端浏览器验证（后台真实回复 → ≤15s 红点出现）需主 Agent/用户人工确认（验收命令 2 的浏览器部分）。
3. 后端回归：未执行（本任务无后端改动，契约明确「后端/接口/字段零改动」；验收命令 3 为可选）。

### 数据完整性
- 未运行任何后端测试，data/ 与 secure/ 未触碰；无残留备份目录。

### 消息收发
- 已读：planner@01a01a02-fd78-70e2-b6fc-97a86b7d684c 的 task-card T20260819-005（版本 1）。
- 已发：dev-ready → docs/coordination/inbox/T20260819-005-developer-01.md（工具不可用降级通道），等待 accept-result。
