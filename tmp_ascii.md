# 异步子 agent 结果汇报与主管获取流程

三张图：派活 / 汇报入账 / 主管取结果的三条路。
对应代码：`graphs/async_task_guard.py`、`custom/outbox_dispatcher.py`、
`custom/delivery_api.py`、`src/aegra_api/services/run_status.py`。

---

## 一、派活：主管把活交出去（`AsyncTaskGuard` 拦截工具调用）

```
 用户             主管(LLM)              AsyncTaskGuard                子 agent 线程
  |                  |                        |                             |
  | "查一下 X"       |                        |                             |
  |----------------->|                        |                             |
  |                  | start_async_task(...)  |                             |
  |                  |----------------------->|                             |
  |                  |                        | 1. _config_for()            |
  |                  |                        |    按 space 收窄子 run 权限   |
  |                  |                        | 2. threads.create(          |
  |                  |                        |      metadata={             |
  |                  |                        |        parent_thread_id: 主管线程id })  <=== 认亲凭据
  |                  |                        | 3. runs.create(             |
  |                  |                        |      input=[description],   |
  |                  |                        |      stream_mode=["messages","custom"])
  |                  |                        |---------------------------->|  跑...
  |                  |                        |                             |
  |                  |<-----------------------|                             |
  |                  | ToolMessage("Launched async subagent. task_id: <子线程id>")
  |                  | state.async_tasks[task_id] = {status: running, run_id, agent_name, ...}
  |                  |                        |                             |
  |                  | ** task_id 就是子线程的 thread_id（1:1，不分家）**
  |                  |                        |                             |
  | "好，我让它去查了"|                        |                             |
  |<-----------------|                        |                             |
  |                  | （主管从不等待，继续干别的活）                          |
```

---

## 二、子 agent 汇报的**唯一锚点**：子 run 到终态时写一行发件箱

```
 worker 跑完子 run
      |
      v
 finalize_run(run_id, 子thread_id, status, ...)
      |
      +--- 同一个事务里做四件事： ----------------------------------+
      |     UPDATE runs    SET status='success'                    |
      |     UPDATE threads SET status='idle'     (无其它活跃 run 才翻) |
      |     读 子线程 metadata.parent_thread_id  <== 认亲唯一凭据     |
      |     INSERT async_task_outbox(                              |
      |         task_id   = 子thread_id,                           |
      |         run_id    = run_id,                                |
      |         parent_thread_id, agent_name, summary)             |
      |         ON CONFLICT (task_id, run_id) DO NOTHING  <== 幂等   |
      +------------------------------------------------------------+
      |
      v
 发件箱行 status='pending'
     |
     +-- status='interrupted'  -> 不插行（这一轮结果不完整，等下一次 run 自己入账）
     +-- summary 列通常是 "(no output)"（子 run 不流 values 事件），
         正文由投递方现查子线程
```

**这一行就是「已兑付」的载体**：跨进程、跨 run 的唯一真相。谁把它从 `pending`
原子翻成 `delivered`，谁就有资格投递。

---

## 三、主管拿到结果的三条路，全汇聚到同一次 claim

```
                     +----------------------------------------------------+
                     |   async_task_outbox 行  (task_id, run_id)           |
                     |   pending --[单行条件 UPDATE]--> delivered          |
                     |   "已兑付"   0 行受影响 = 别人已经投了              |
                     +----------------------------------------------------+
                          ^                ^                   ^
                          |                |                   |
        (A) 父 run 开始    |   (B) 父线程空闲 |   (C) 模型主动要    |
                          |                |                   |

[A] 钩子：run 开始前已终态的结果
 主管这一轮要说话了
      |
      v
 AsyncTaskGuard.abefore_agent
      |  1. _refreshed_async()   问每个没终态的 task 真实状态（state 里的只是快照）
      |  2. _plan()              只看 delivered_run_id 决定"要不要投"
      |                           （不扫 messages：摘要会删、模型能伪造）
      |  3. _summary_async()     查子线程最后一条消息（5s 超时）<== 慢的放最前
      |  4. POST /custom/tasks/{t}/deliver-claim
      |         True  -> 有资格，投
      |         False -> 别人投过：跳过，并把 delivered_run_id 写回 state（自校准）
      |         None  -> 不知道：**不投**，行留 pending，下一轮再试
      v
 本次 run 的 state 多一条 human 消息：
    "[Async subagent 'x' has completed] Result: <前500字>...
                                       task_id: t
                                       run_id:  r"
 并记 async_tasks[t].delivered_run_id = r

[B] 分发者：run 期间完成 / 主管已空闲
 每个 worker 进程一个循环，每 1s：
      |
      |  1. 领批：SELECT ... FOR UPDATE SKIP LOCKED  + 租约(lease_expires_at)
      |  2. 按 parent_thread_id 分组
      |  3. SELECT thread FOR UPDATE（关掉 check-then-act 竞态）
      |        |
      |        +--- 该父线程有 pending/running 的 run 行？
      |        |        |
      |        |        是 --> 退避：next_attempt_at += 2s（不记失败次数）
      |        |        否
      |        v
      |  4. 开一轮唤醒 run：
      |       run_id = uuid5(parent + 排序后的 (task,run))   <== 确定性
      |       input  = 本批结果消息拼在一起
      |       撞主键 = 上次已经建过 -> 按已投递处理（不开第二轮）
      |  5. 成功后把行标 delivered
      v
 主管被"唤醒"跑一轮，模型对结果说一句话

[C] 模型自救：run 跑到一半就要结果
 模型调 check_async_task(task_id)
      |
      +--> 现查子线程 --> 返回**全文**（不截断，和卡片的 500 字不同）
      |
      v
 _amark_checked：
      记 delivered_run_id  +  立刻 claim 那一行（否则分发者 run 结束后会再送一遍）
```

---

## 四、谁写什么（改代码前先对这张）

```
 位置                                   写什么                                性质
 ------------------------------------   -----------------------------------   ------------------
 子线程 metadata.parent_thread_id       父子关系                            决定 finalize_run 认不认亲
 async_task_outbox.status               pending -> delivered                 已兑付：唯一真相
 父 state .async_tasks[t].delivered_run_id  这次 run 的结果我有了           已入上下文：本地缓存，
                                                                             丢了能重建（多一次 claim）
 父 state .messages 里那张卡片          给前端渲染的 UI 产物                  不是判据（摘要删得掉、
                                                                             模型伪造得了）
```

## 五、三种"结果没出现"的边界

```
 场景                          行为                                              结果
 ----------------------------  ------------------------------------------------  -------------------
 主管正在跑，结果刚好完成         guard 不动手（没有 after_agent），分发者退避      多花一轮 LLM，
                               直到 run 结束                                    模型能对结果说话
 主管 interrupted（等用户回答）   谁都不投（硬 resume 会顶掉用户要答的内容）        用户答完后下一个
                                                                                run 的钩子补上
 update_async_task 换了新 run    新 run_id = 新主键 = 新的一行                      照常再投一次
                                                                               （有意为之）
```

一句话概括：**子 agent 只做一件事——终态时留一行发件箱；三个投递方都只做一件事——
抢那一行的翻牌权；主管只有三个时刻能看到结果——本轮 run 开始、被唤醒的 run、自己 check。**
