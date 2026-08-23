// =============================================================================
// 社区网格助手 · k6 压测脚本（问题 15：并发压力测试）
// -----------------------------------------------------------------------------
// 运行方式（需先安装 k6：https://k6.io/docs/get-started/installation/）：
//
//   # 混合模式（健康检查 + 注册/登录 + 事件提交 + 事件列表）：
//   k6 run -e BASE_URL=http://127.0.0.1:8000 -e VUS=50 -e DURATION=2m scripts/loadtest/k6-loadtest.js
//
//   # 只读模式（健康检查 + 首页静态资源，不触发登录/事件限流）：
//   k6 run -e BASE_URL=http://127.0.0.1:8000 -e MODE=read scripts/loadtest/k6-loadtest.js
//
// 环境变量：
//   BASE_URL  服务地址，默认 http://127.0.0.1:8000
//   VUS       并发虚拟用户数，默认 50
//   DURATION  稳态压测时长，默认 2m
//   RAMP      爬坡时长，默认 30s
//   MODE      read（只读）| mixed（读写，默认）
//
// ⚠️ 限流说明：应用自带 slowapi 限流（登录/注册 5/min/IP、事件 10/min/用户）。
//    混合模式压测大量注册/登录会触发 429——那是限流在正常工作。
//    压测目标是「应用容量」而非「限流本身」（限流有独立单测覆盖），
//    因此混合模式压测前建议临时在 .env 设置 RATE_LIMIT_ENABLED=false 并重启；
//    只读模式无需关闭。
// =============================================================================
import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:8000";
const VUS = Number(__ENV.VUS || 50);
const DURATION = __ENV.DURATION || "2m";
const RAMP = __ENV.RAMP || "30s";
const MODE = (__ENV.MODE || "mixed").toLowerCase();

export const options = {
  scenarios: {
    load: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: RAMP, target: VUS },
        { duration: DURATION, target: VUS },
        { duration: "10s", target: 0 },
      ],
      gracefulRampDown: "10s",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.05"],       // 请求失败率 < 5%
    http_req_duration: ["p(95)<2000"],    // 95% 请求 < 2s
  },
};

const jsonHeaders = { "Content-Type": "application/json" };

// 健康检查（无鉴权、不参与业务限流）
function checkHealth() {
  const res = http.get(`${BASE_URL}/health`);
  check(res, { "GET /health 200": (r) => r.status === 200 && r.json("status") === "ok" });
}

// 写路径：注册/登录获取 token，提交事件，读取列表
function mixedFlow() {
  const uid = `${__VU}-${__ITER}`;
  const username = `k6_${uid}`;
  const password = "K6test123!";

  // 1) 注册（每个 VU 每次迭代独立账号，避免用户名冲突）
  let token = "";
  const reg = http.post(
    `${BASE_URL}/api/auth/register`,
    JSON.stringify({ username, password }),
    { headers: jsonHeaders }
  );
  token = reg.json("data.token") || "";

  // 2) 注册被拒（如账号已存在）则改为登录
  if (!token) {
    const login = http.post(
      `${BASE_URL}/api/auth/login`,
      JSON.stringify({ username, password }),
      { headers: jsonHeaders }
    );
    token = login.json("data.token") || "";
  }
  check(token !== "", { "获得登录 token": token !== "" });

  const authHeaders = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  };

  // 3) 提交事件
  const event = http.post(
    `${BASE_URL}/api/events`,
    JSON.stringify({
      description: `压测事件 ${uid}：小区路灯不亮，存在安全隐患`,
      address: "幸福小区3栋502室",
      event_type: "安全隐患",
      urgency: "中",
    }),
    { headers: authHeaders }
  );
  check(event.status === 200 || event.status === 202, { "POST /api/events 200/202": event.status === 200 || event.status === 202 });

  // 4) 读取事件列表
  const list = http.get(`${BASE_URL}/api/events`, { headers: authHeaders });
  check(list.status === 200, { "GET /api/events 200": list.status === 200 });
}

export default function () {
  checkHealth();
  if (MODE !== "read") {
    mixedFlow();
  }
  sleep(1);
}