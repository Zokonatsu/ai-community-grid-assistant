// 审查工作流：多维度并行审查 → 对抗验证 → 汇总排序
// 用法：主 Agent 显式请求（"use a workflow"/"多 agent 审查"）时运行。
// args: { focus?: string 审查重点, files?: string[] 限定文件（可选，默认由审查者自行定位） }

export const meta = {
  name: 'review-changes',
  description: '并行多维度审查本项目改动，对抗验证后返回严重度排序的 findings',
  phases: [
    { title: 'Review', detail: '4 个维度并行审查同一批改动' },
    { title: 'Verify', detail: '每条 finding 由独立 agent 尝试反驳' },
  ],
}

const FINDINGS_SCHEMA = {
  type: 'object',
  required: ['findings'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'file', 'line', 'summary', 'category'],
        properties: {
          severity: { enum: ['high', 'medium', 'low'] },
          file: { type: 'string' },
          line: { type: 'number' },
          summary: { type: 'string' },
          category: { type: 'string' }, // correctness | security | performance | contract
          failure_scenario: { type: 'string' },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  required: ['refuted', 'reason'],
  properties: {
    refuted: { type: 'boolean' },
    reason: { type: 'string' },
  },
}

const DIMENSIONS = [
  {
    key: 'correctness',
    prompt: (focus) => `你是只读代码审查者。审查本项目最近的改动，只找【正确性】类问题：逻辑错误、边界条件、状态机流转、异常处理缺失、空值/NPE。${focus ? `重点：${focus}。` : ''}带具体文件:行号。不做任何修改。`,
  },
  {
    key: 'security',
    prompt: (focus) => `你是只读安全审查者。审查本项目最近的改动，只找【安全】类问题：XSS（innerHTML 是否过 escapeHtml）、越权（居民 vs 管理员 403）、密钥泄露、注入、路径穿越。${focus ? `重点：${focus}。` : ''}带具体文件:行号。不做任何修改。`,
  },
  {
    key: 'performance',
    prompt: (focus) => `你是只读性能/并发审查者。审查本项目最近的改动，只找【性能/并发】类问题：阻塞操作放在 HTTP 请求内、锁使用不当、后台任务超时/泄漏、线程池耗尽。${focus ? `重点：${focus}。` : ''}带具体文件:行号。不做任何修改。`,
  },
  {
    key: 'contract',
    prompt: (focus) => `你是只读契约审查者。审查本项目最近的改动，只找【契约对齐】问题：前端 fetch 的字段名/类型/URL/方法与后端路由是否一致，INTERFACE.md 是否仍成立，前端 escapeHtml 使用是否一致。${focus ? `重点：${focus}。` : ''}带具体文件:行号。不做任何修改。`,
  },
]

phase('Review')
const focus = (args && args.focus) || ''
const results = await pipeline(
  DIMENSIONS,
  (d) =>
    agent(d.prompt(focus), {
      label: `review:${d.key}`,
      phase: 'Review',
      schema: FINDINGS_SCHEMA,
    }),
  (review) => {
    if (!review || !review.findings || !review.findings.length) return []
    return parallel(
      review.findings.map((f) => () =>
        agent(
          `你是对抗验证者。审查者提出以下 finding，请尝试【反驳】它：\n` +
            `文件 ${f.file}:${f.line} — ${f.summary}\n` +
            `失败场景: ${f.failure_scenario || '未给出'}\n\n` +
            `读相关代码核实。若它其实是误报（行为正确、无真实危害、或触发条件不可能），refuted=true；` +
            `若确为真实问题，refuted=false。不确定时倾向 refuted=true（宁可漏报不可误报）。`,
          { label: `verify:${f.file}:${f.line}`, phase: 'Verify', schema: VERDICT_SCHEMA }
        ).then((v) => ({ ...f, verdict: v }))
      )
    )
  }
)

const flat = results.filter(Boolean).flat()
const confirmed = flat.filter((f) => f.verdict && !f.verdict.refuted)
const ORDER = { high: 0, medium: 1, low: 2 }
confirmed.sort((a, b) => ORDER[a.severity] - ORDER[b.severity])
log(`审查完成：${flat.length} 条 finding，对抗验证后确认 ${confirmed.length} 条`)
return { confirmed, all: flat }
