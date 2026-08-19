# 事件提交后大模型处理 · 开发手册

> 定位：面向**开发者 / 开发代理**维护「事件提交后的大模型语义处理链路」的规范文档。
> 事实源：代码优先（函数名/行号以当前仓库为准）；本文档描述**判定规范、代码位置与改法**。
> 范围：仅覆盖 `POST /api/events` 之后的大模型处理（接收/派单/记录），不含注册定位、登录等其它模块。

---

## 1. 处理链路总览

```
POST /api/events            main.py:636  create_event
  │
  ├─① 前置硬规则     _check_hard_rules_first   receive_agent.py:147   （命中→跳过 LLM）
  ├─② 前置模糊急救   _check_fuzzy_emergency    receive_agent.py:85    （短词高风险→二次确认）
  ├─③ 机械预检       _is_valid_input           receive_agent.py:357   （纯规则，不调 LLM）
  ├─④ LLM 语义判断   receive_node              receive_agent.py:432   （一次思考，单次调用）
  ├─⑤ 安全兜底       （无效→待审核/无效输入）
  ├─⑥ 派单（纯规则） dispatch_node             dispatch_agent.py:108
  └─⑦ 记录           record_node               record_agent.py:97 + main.py _save_tasks
```

**同步 / 异步**：
- ④ 语义判断在 `create_event` 内**同步**执行（`asyncio.wait_for(..., timeout=50.0)`，main.py:778），响应里返回 `event_type/urgency/scene_tag`。
- ⑥⑦ 派单+记录在后台 `_process_event`（main.py:122）**异步**执行，因此 POST 响应中 `handler` 为空、`status=处理中`；最终结果需查 `GET /api/events`。

---

## 2. 各层判定规范

### 2.1 机械预检（纯规则，不调 LLM）
- 函数：`_is_valid_input(description)` receive_agent.py:357；词表 `_INVALID_INPUT_RE` :349
- 拦截条件（返回 `event_type="无效输入"`、`urgency=低`、`scene_tag=常规`）：
  - 空串 / 纯空白
  - 纯数字 / 纯标点（无字母汉字）
  - 长度 < 2 字（仅 1 字拒绝；2 字如「漏水/停电」交 LLM 判断）
  - 纯问候/闲聊/测试词表：你好|您好|哈喽|hi|hello|在吗|在么|有人吗|早上好|下午好|晚上好|晚安|再见|拜拜|谢谢|感谢|你们好|大家好|吃了吗|test|测试|hello world|123+|哈哈哈+|嘻嘻+|呵呵+|呜呜+|嗯+|啊+|哦+|哇+|哎+
  - 迷信/超自然词表（`_SUPERNATURAL_RE`）：有鬼|闹鬼|见鬼|撞鬼|鬼影|鬼魂|鬼火|鬼上身|鬼压床|妖魔鬼怪|妖怪|邪灵|中邪|附身|僵尸|幽灵|托梦|前世|来世|阴间|神婆|跳大神|驱鬼|招鬼|降头…（命中直接拒绝，防 LLM 随机误判为精神急救/受理）
- **例外优先放行**：命中生命急救/紧急救援/模糊急救关键词时不受长度限制，直接放行。
- **后端长度上限**：`EventRequest.description`（main.py:301）`max_length=500`，超长返回 422。

### 2.2 前置硬规则（跳过 LLM，零延迟）
- 函数：`_check_hard_rules_first(description)` receive_agent.py:147
- 词表：`_LIFE_RESCUE_RE` :59（心脏骤停|大出血|昏迷|割腕|自杀|溺水…）、`_EMERGENCY_RESCUE_RE` :64（火灾|燃气泄漏|电梯困人|坍塌|爆炸|高空坠物…）
- 命中返回**固定结构化结果**（不调用 LLM）：
  - 生命急救 → `event_type=安全隐患 / urgency=高 / scene_tag=生命急救 / emergency_type=medical`
  - 紧急救援 → `event_type=安全隐患 / urgency=高 / scene_tag=紧急救援 / emergency_type=fire`
- 前端未确认（`confirmed=false`）→ 返回 `confirmation_required=true` **弹窗确认，不建单**；确认后才建单并后台派单（main.py:674-731）。

### 2.3 前置模糊急救（短词二次确认）
- 函数：`_check_fuzzy_emergency(description)` receive_agent.py:85
- 触发：描述**长度 ≤ 4 字**且命中 `_FUZZY_MEDICAL/POLICE/FIRE_RE`（吐血|晕倒|**救命|救我|呼救**|着火|被困|**救火**|绑架|抢劫…）
- 行为：返回 `confirmation_required=true + emergency_type`（medical/police/fire），前端二次确认后带 `confirmed=true&emergency_type` 重新提交。

### 2.4 LLM 语义判断（一次思考）
- 入口：`receive_node(state)` receive_agent.py:432
- **多轮投票**：`SEMANTIC_CHECK_ROUNDS = 3` receive_agent.py:51；`ThreadPoolExecutor(max_workers=3)` **并行**调用 3 次 `_call_llm_once(description)` :182，投票消除边界输入（迷信/闲聊）的判定随机性
- `_call_llm_once`：DeepSeek `deepseek-chat` + `RECEIVE_SYSTEM_PROMPT`(:294) + 描述；`response_format=json_object`；单次 `timeout=15s`
- 结果处理链：`_apply_hard_rules`(:101) 硬规则兜底覆盖 → `_vote_on_results`(:207)（单结果时直接返回，`confidence=medium`）
- **判定 `is_valid` 后**：
  - `true` → 提取 `address / event_type / urgency / scene_tag` 交派单
  - `false` → 安全兜底：
    - 命中急救/救援词 → `event_type=待审核`（转人工部）
    - 其它（含短词乱打/闲聊）→ `event_type=无效输入`（直接拒绝，带 `reject_reason`，不进审核）
  - 调用异常 → `event_type=API异常`（main 转待审核/人工部，文案「语义校验服务异常，已转人工审核」）

### 2.5 提示词规范（RECEIVE_SYSTEM_PROMPT :294）
- 结构：职责范围（正面/负面清单）→ 判断规则 → few-shot 示例 → 字段要求 → 输出格式
- 负面清单（is_valid=false）：个人私事（买菜/做饭/出行/逛街）、医疗诊断、商业交易、超社区范围、日常闲聊、无意义问候/测试字符串、家养宠物死亡、个人投资理财、纯个人情感倾诉
- **安全红线**：涉及人身安全/医疗急救/死亡/火灾/燃气泄漏，无论多简短都强制 `is_valid=true`
- 字段契约：`is_valid`(bool) / `reject_reason`(string) / `address`(string) / `event_type`(6 类枚举) / `urgency`(高|中|低) / `scene_tag`(生命急救|紧急救援|常规)
- 输出：必须合法 JSON，无 Markdown 代码块

### 2.6 派单规范（纯规则，dispatch_agent.py）
- 函数：`dispatch_node(state)` dispatch_agent.py:108
- 优先级（从高到低）：
  1. `emergency_type`（medical/police/fire，来自模糊急救确认）→ 120/110/119 外部资源
  2. `scene_tag=生命急救` → 120医疗急救中心（外部资源）
  3. `scene_tag=紧急救援` → 按关键词推断 119消防 / 110公安 / 120医疗
  4. `scene_tag=常规` → `EVENT_TYPE_TO_HANDLER`(:61) 映射表
  5. `emergency_type=人工部` 或 `event_type=待审核` → 人工部
  6. 未匹配兜底 → 综合部
- 映射表（:61）：物业维修→物业部；环境卫生→环卫部；安全隐患→安保部；邻里纠纷→调解员；公共设施→工程部；其他→综合部
- 附加：`urgency=高` 且 `scene_tag=常规` → handler 加 `[紧急]` 前缀

### 2.7 状态机与错误文案
- 任务状态：处理中 / 已完成 / 待审核 / 已受理 / 处理超时 / 处理失败（main.py 任务字典 `status`）
- 固定文案：
  - 语义校验超时 → 待审核，error「语义校验超时，已转人工审核」
  - 语义校验异常 → 待审核，error「语义校验服务异常，已转人工审核」
  - 处理超过 60s → 处理超时，error「AI 处理超过60秒，已超时」

---

## 3. 扩展开发规范（改法）

### 3.1 新增/修改关键词
| 目标 | 位置 | 注意 |
|---|---|---|
| 生命急救硬规则词 | receive_agent.py:59 `_LIFE_RESCUE_RE` | 新增词影响所有事件；改后跑 test_comprehensive、test_life_rescue_fix |
| 紧急救援硬规则词 | receive_agent.py:64 `_EMERGENCY_RESCUE_RE` | 同上 |
| 模糊急救短词 | receive_agent.py:72-82 `_FUZZY_*_RE` | 只对 ≤4 字短词生效 |
| 问候/无效词 | receive_agent.py:349 `_INVALID_INPUT_RE` | 只影响机械预检层 |
| 派单兜底推断词 | dispatch_agent.py:88 `_infer_emergency_type` | 二次提交兜底恢复 emergency_type |

### 3.2 修改 LLM 提示词
- 位置：receive_agent.py:294 `RECEIVE_SYSTEM_PROMPT`
- 注意：
  - **必须保持 JSON 输出契约**（字段名/类型不变），否则解析失败走 API异常
  - 新增字段需同步 `ReceiveState`/`WorkflowState` 与下游消费方
  - few-shot 示例是行为基准，增删会直接改变判定结果，改后必须跑全量回归
- 验证：test_comprehensive.py（workflow 集成）、test_semantic_timeout.py（超时/异常）

### 3.3 新增事件类型 / 处理部门
1. 提示词 `event_type` 枚举（receive_agent.py:294）
2. 派单映射 `EVENT_TYPE_TO_HANDLER`（dispatch_agent.py:61）
3. 契约文档 `docs/INTERFACE.md`
4. 前端展示（如需显示新类型/部门）
- 约束：不要新增的 event_type 与已有语义重叠；「其他」只能兜底，严禁把个人私事归入

### 3.4 一次思考 vs 多轮投票
- `SEMANTIC_CHECK_ROUNDS`（receive_agent.py:51）：当前 `3`=多轮并行投票（总耗时 ≈ 单次）；曾临时改 `1` 作「一次思考」，因边界输入判定随机（如「我家有鬼」忽无效忽急救）而恢复 `3`
- 投票逻辑 `_vote_on_results`(:207)：投票键 `(is_valid, event_type, urgency, scene_tag)`；多结果时全一致→high、多数(≥2)→medium、分散→low；单结果固定 medium
- 改轮数后注意置信度语义变化；超时配置 `_SEMANTIC_SINGLE_TIMEOUT=15.0`(:52) 与 main.py:780 `timeout=50.0` 保持配套

### 3.5 修改错误文案 / 状态
- 文案集中在 main.py `create_event` 的异常分支（超时/API异常）与 `_process_event`（超时/失败）
- 改文案后跑 test_semantic_timeout.py（断言固定文案）

---

## 4. 测试与验收

### 相关测试
| 测试 | 覆盖 |
|---|---|
| tests/test_comprehensive.py | 工作流集成、硬规则短路（0 次 LLM 调用）、关键词覆盖、无效输入拦截 |
| tests/test_semantic_timeout.py | 语义校验超时/API异常→待审核、固定文案、wait_for 配置 |
| tests/test_life_rescue_fix.py | 生命急救硬规则 |
| tests/test_scene_tag.py | 场景标签判定 |
| tests/test_proxy_beneficiary.py | 本人/代人办提交（走事件链路） |
| tests/test_register_location.py | 注册定位范围校验（全量回归覆盖，非事件 LLM） |

### 运行方式（铁律）
1. 用 venv 绝对路径：`C:\Users\78397\Desktop\ai-community-grid-assistant\.venv\Scripts\python.exe tests\<name>.py`
2. **先备份 `data/` 与 `secure/`**（测试脚本会备份/恢复；`test_input_validation.py` 存在 GBK/emoji 崩溃导致备份被清理的历史 bug，重跑前人工再备一份）
3. **逐个串行运行，禁止并行**；`PYTHONIOENCODING=utf-8` 避免控制台编码崩溃
4. 跑完核对 `secure/users.json.enc` 仍含 admin 账号（解密核对，勿打印密钥）
5. `tests/run_tests.py` 是**实时冒烟脚本**（curl 8000），不是全量回归；全量回归=逐个跑 `tests/test_*.py`

### 收口检查清单
- `python -m py_compile` 改动文件
- 相关测试全绿 + 全量回归串行通过
- 改前端时：抽取内联 `<script>` → `node --check`
- 行为验证：提交 1 条普通事件 + 1 条高危事件（确认流程）实测

---

## 5. 关键文件索引

| 文件 | 职责 | 关键函数/常量 |
|---|---|---|
| receive_agent.py | 接收/语义提取（唯一调 LLM） | `receive_node`:432、`_call_llm_once`:182、`_check_hard_rules_first`:147、`_check_fuzzy_emergency`:85、`_apply_hard_rules`:101、`_vote_on_results`:207、`_is_valid_input`:357、`RECEIVE_SYSTEM_PROMPT`:294、`SEMANTIC_CHECK_ROUNDS`:51 |
| dispatch_agent.py | 派单（纯规则，无 LLM） | `dispatch_node`:108、`EVENT_TYPE_TO_HANDLER`:61、`_infer_emergency_type`:88 |
| workflow.py | 组装 LangGraph | `workflow`（receive→dispatch→record）、`dispatch_record_workflow`（dispatch→record） |
| record_agent.py | 持久化 | `record_node`:97 |
| main.py | FastAPI 入口/状态机 | `create_event`:636、`_process_event`:122、`_build_task`:197、任务字典 `_tasks` |
| geo.py | 定位/范围（注册用，事件仅透传） | `is_within_community`、`get_community_config` |
| docs/INTERFACE.md | 接口契约（事实源） | 改接口时同步更新 |

---

## 6. 环境与配置

- LLM：`config.LLM_API_KEY` / `config.LLM_BASE_URL`（读 `.env`），模型 `deepseek-chat`；**服务进程需能访问 `api.deepseek.com`**（沙箱/防火墙限制会导致 LLM 静默失败走兜底）
- 启动：`C:\Users\78397\Desktop\ai-community-grid-assistant\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000`
- 密钥安全：`LLM_API_KEY` / `DATA_ENCRYPTION_KEY` 不写入本文档/知识库，引用见各 `.env`

---

## 7. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-08-19 | 初版手册；记录「一次思考」：`SEMANTIC_CHECK_ROUNDS` 3→1（单次 LLM 调用），投票函数保留用于恢复多轮 |
| 2026-08-19 | 移除「短词(≤4字)无效→待审核」兜底：LLM 判无效即拒绝（乱打字/闲聊不再进后台审核）；真实紧急短词仍由前置硬规则/模糊急救拦截 |
| 2026-08-19 | 覆盖缺口修复：① 求救词（救命/救我/呼救/救火）加入模糊急救词表；② 机械预检长度 3→2（漏水/停电等 2 字合法短报交 LLM）；③ 描述加 `max_length=500` |
| 2026-08-19 | 恢复 3 轮投票（`SEMANTIC_CHECK_ROUNDS` 1→3）消除边界输入随机性；新增迷信/超自然词表 `_SUPERNATURAL_RE`（有鬼/闹鬼/妖怪…）直接判无效 |