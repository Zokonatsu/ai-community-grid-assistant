# 开发协作方式 · 多 Agent

本项目用 **Claude Code 的多 Agent 协作**来开发。主 Agent（Claude）负责拆任务、冻结契约、收口合并；子 Agent 按角色并行产出。**这套协作只影响开发过程，与运行时无关**——应用内部的事件流水线（receive/dispatch/record）仍是串行的。

## 一、团队角色（`.claude/agents/`）

| 角色 | 文件 | 管什么 |
|---|---|---|
| 后端实现 | `.claude/agents/backend-dev.md` | FastAPI 后端（main.py、*_agent.py、geo、community_store、auth、config） |
| 前端实现 | `.claude/agents/frontend-dev.md` | static/ 下 HTML + CSS + 内联 JS，对接后端 API 契约 |
| 测试编写 | `.claude/agents/test-writer.md` | tests/*.py 独立脚本（数据隔离 + mocked LLM） |
| 代码审查 | `.claude/agents/code-reviewer.md` | 只读审查，多维度找 bug/缺口，不做修改 |

角色文件里写了各自的**项目约定 + 验证方式**，子 Agent 开工前读自己的角色文件。

## 二、什么时候用多 Agent（收益明显）

1. **跨模块功能**：改接口同时要改页面 → 先由主 Agent 冻结**契约**（URL、方法、请求/响应字段），再并行派 `frontend-dev` 改页面 + `backend-dev` 改接口 + `test-writer` 写测试，三者按同一契约各自独立实现，最后主 Agent 收口对齐。
2. **审查**：派 `code-reviewer` 并行按不同维度（正确性/安全/性能/契约对齐）审查同一批改动，主 Agent 汇总排序。
3. **测试套件**：多个 `tests/*.py` 互不依赖，可并行派 `test-writer` 补不同用例。

## 三、什么时候仍用单 Agent（避免误用）

- 单文件小改动（修 typo、改一个字段名）——直接改，不建团队。
- 强耦合的前后端小改——串行即可，不值得拆。
- 探索/查问题——用 `Explore` agent 单派。

## 四、协作纪律（铁律）

- **契约先行**：多 Agent 改共享接口时，主 Agent 先冻结字段契约，子 Agent 按契约实现，不擅自发明字段。
- **文件隔离**：子 Agent 默认不共享同一文件；确需并行改同一文件时用 `isolation: worktree` 隔离，避免互相覆盖。
- **主 Agent 收口**：子 Agent 只产出，**合并 / 对齐 / 提交由主 Agent 负责**。子 Agent 不 git commit/push。
- **只读审查**：`code-reviewer` 永不改文件。
- **回归不破**：任何后端/前端改动，收口前跑对应测试（见各角色文件「验证方式」）。

## 五、提交规范

- 提交信息：`<type>: <简述>`（如 `feat:`, `fix:`），中文描述，多行信息用临时文件 `-F` 或字符串数组拼接，**不用 here-string**（PowerShell 5.1 会解析破坏）。
- 分支：功能分支（如 `feature/zhang-collapse-beautiful`）→ 合入 `main`。
- 提交前跑 `python tests/run_tests.py` 全量回归。

## 六、契约文档

`INTERFACE.md` 是模块接口约定的事实源；`tests/` 每个脚本顶部 docstring 是它的测试范围说明。改接口时同步更新 `INTERFACE.md`。
