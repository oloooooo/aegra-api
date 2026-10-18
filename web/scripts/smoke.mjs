/**
 * 前端 API 契约冒烟测试：用与 src/ 完全一致的请求形状跑一遍后端。
 *
 *   node scripts/smoke.mjs
 *
 * 覆盖：登录 → whoami → graphs → spaces/files/users → 建会话 → SSE 流
 * （messages/updates/values）→ 追问 interrupt → resume → 任务代理（状态 + SSE）
 * → 定时任务 CRUD → 删会话。
 */

const BASE = process.env.AEGRA_API ?? "http://127.0.0.1:2026";
const USER = process.env.AEGRA_USER ?? "root";
const PASS = process.env.AEGRA_PASS ?? "1234";

let token = "";
const ok = [];
const bad = [];
const check = (name, cond, extra = "") => (cond ? ok.push(name) : bad.push(`${name} ${extra}`));

const headers = (extra = {}) => ({ "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...extra });

async function api(path, init = {}) {
  const res = await fetch(BASE + path, { ...init, headers: headers(init.headers) });
  const text = await res.text();
  if (!res.ok) throw new Error(`${init.method ?? "GET"} ${path} → ${res.status} ${text.slice(0, 200)}`);
  return text ? JSON.parse(text) : null;
}

/** 与 src/api/client.ts 的 sse() 同一套解析逻辑。 */
async function* sse(path, init = {}) {
  const res = await fetch(BASE + path, { ...init, headers: headers({ Accept: "text/event-stream", ...(init.headers ?? {}) }) });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      let event = "message";
      const data = [];
      for (const line of raw.split("\n")) {
        if (!line || line.startsWith(":")) continue;
        const i = line.indexOf(":");
        const f = i < 0 ? line : line.slice(0, i);
        const v = i < 0 ? "" : line.slice(i + 1).replace(/^ /, "");
        if (f === "event") event = v;
        else if (f === "data") data.push(v);
      }
      if (!data.length) continue;
      try {
        yield { event, data: JSON.parse(data.join("\n")) };
      } catch {
        yield { event, data: data.join("\n") };
      }
    }
  }
}

const lastText = (data) => {
  const arr = Array.isArray(data) ? data : [data];
  let last = arr[arr.length - 1];
  if (Array.isArray(last)) last = last[0];
  if (!last || typeof last !== "object") return "";
  const c = last.content;
  return typeof c === "string" ? c : Array.isArray(c) ? c.map((x) => x?.text ?? "").join("") : "";
};

const main = async () => {
  // 1. 登录
  const login = await api("/custom/login", { method: "POST", body: JSON.stringify({ identity: USER, password: PASS }) });
  token = login.access_token;
  check("login", !!token);
  check("whoami", (await api("/custom/whoami")).identity === USER);
  check("user/spaces", Array.isArray((await api("/custom/user/spaces")).space));

  // 2. 图清单
  const g = await api("/custom/graphs");
  check("graphs 只含注册图", g.graphs.includes("supervisor") && g.graphs.length === 4, JSON.stringify(g.graphs));

  // 3. 空间
  const sp = await api("/custom/spaces");
  check("spaces 列表", sp.spaces.length > 0);
  const name = sp.spaces.find((s) => s.accessible)?.name;
  const files = await api(`/custom/spaces/${encodeURIComponent(name)}/files`);
  check("spaces/files", Array.isArray(files.files) && files.files.length > 0, name);
  check("spaces/users", Array.isArray((await api(`/custom/spaces/${encodeURIComponent(name)}/users`)).users));

  // 4. 建会话 + 流式一轮（会派子 agent）
  const th = await api("/threads", { method: "POST", body: JSON.stringify({}) });
  const threadId = th.thread_id;
  let sawPartial = false;
  let sawTool = false;
  let taskId = null;
  for await (const ev of sse(`/threads/${threadId}/runs/stream`, {
    method: "POST",
    body: JSON.stringify({
      assistant_id: "supervisor",
      input: { messages: [{ role: "user", content: "CBAM 默认排放因子里铝的因子是多少？" }] },
      stream_mode: ["messages", "updates", "values", "custom"],
    }),
  })) {
    if (ev.event === "messages/partial" && lastText(ev.data)) sawPartial = true;
    if (ev.event === "custom") sawTool = true;
    if (ev.event === "values" && ev.data?.async_tasks) taskId = Object.keys(ev.data.async_tasks).at(-1) ?? taskId;
    if (ev.event === "end") break;
  }
  check("主对话流有 token", sawPartial);
  check("拿到 async_tasks", !!taskId);

  // 5. 任务代理
  const st = await api(`/custom/tasks/${taskId}?parent_thread_id=${threadId}`);
  check("任务状态已核实", ["running", "success", "pending", "error"].includes(st.status), st.status);
  let taskEvents = 0;
  let taskText = false;
  for await (const ev of sse(`/custom/tasks/${taskId}/stream?parent_thread_id=${threadId}`, { method: "GET" })) {
    if (ev.event === "custom") taskEvents++;
    if (ev.event === "messages/partial" && lastText(ev.data)) taskText = true;
    if (ev.event === "end") break;
  }
  check("子 agent 流有 custom 事件", taskEvents > 0, String(taskEvents));
  check("子 agent 流有 token", taskText);

  // 6. 追问 → resume（另起一轮，故意问得含糊）
  const th2 = (await api("/threads", { method: "POST", body: JSON.stringify({}) })).thread_id;
  let interrupt = null;
  for await (const ev of sse(`/threads/${th2}/runs/stream`, {
    method: "POST",
    body: JSON.stringify({
      assistant_id: "supervisor",
      input: { messages: [{ role: "user", content: "那个表里有什么" }] },
      stream_mode: ["updates"],
    }),
  })) {
    if (ev.event === "updates" && ev.data?.__interrupt__) interrupt = ev.data.__interrupt__[0];
    if (ev.event === "end") break;
  }
  const stateAfter = await api(`/threads/${th2}/state`);
  check("interrupt 可从 state 读到", Array.isArray(stateAfter.interrupts) && stateAfter.interrupts.length > 0);
  check("interrupt 带 options", !!interrupt?.value?.options?.length);
  const label = interrupt?.value?.options?.[0]?.label ?? "第一个表";
  const resumed = await api(`/threads/${th2}/runs`, {
    method: "POST",
    body: JSON.stringify({ assistant_id: "supervisor", command: { resume: label } }),
  });
  check("resume 被接受", !!resumed.run_id);

  // 7. 定时任务 CRUD
  const th3 = (await api("/threads", { method: "POST", body: JSON.stringify({}) })).thread_id;
  await api(`/threads/${th3}/runs/crons`, {
    method: "POST",
    body: JSON.stringify({
      assistant_id: "supervisor",
      schedule: "0 9 * * *",
      timezone: "Asia/Shanghai",
      input: { messages: [{ role: "user", content: "冒烟测试：只回复 OK" }] },
    }),
  });
  let crons = await api("/runs/crons/search", { method: "POST", body: JSON.stringify({ limit: 50 }) });
  const mine = crons.find((c) => c.thread_id === th3);
  check("cron 已创建", !!mine);
  if (mine) {
    await api(`/runs/crons/${mine.cron_id}`, { method: "PATCH", body: JSON.stringify({ enabled: false }) });
    crons = await api("/runs/crons/search", { method: "POST", body: JSON.stringify({ limit: 50 }) });
    check("cron 可暂停", crons.find((c) => c.cron_id === mine.cron_id)?.enabled === false);
  }

  // 8. 删会话（级联删 cron）
  await api(`/threads/${th3}`, { method: "DELETE" });
  const gone = await fetch(`${BASE}/threads/${th3}`, { headers: headers() });
  check("会话已删除", gone.status === 404);
  crons = await api("/runs/crons/search", { method: "POST", body: JSON.stringify({ limit: 50 }) });
  check("cron 随会话级联删除", !crons.some((c) => c.thread_id === th3));

  // 9. 越权：拿别人的 parent_thread_id
  const denied = await fetch(`${BASE}/custom/tasks/${taskId}?parent_thread_id=00000000-0000-0000-0000-000000000000`, { headers: headers() });
  check("任务代理拒绝陌生父会话", denied.status === 404, String(denied.status));

  console.log(`\n通过 ${ok.length}/${ok.length + bad.length}`);
  if (bad.length) {
    console.log("失败：");
    for (const b of bad) console.log("  ✗", b);
    process.exit(1);
  }
  console.log("全部通过 ✔");
};

main().catch((e) => {
  console.error("冒烟测试异常：", e.message);
  process.exit(1);
});
