 简报：ORM 说法已统一

 ### 定的词（已写进 CONTEXT.md）

 ┌─────────────────────────────┬──────────────────────────────────────┬──────────────────┐
 │ 词                          │ 含义                                 │ 是否落库         │
 ├─────────────────────────────┼──────────────────────────────────────┼──────────────────┤
 │ Microservice（微服务）      │ 用户拥有的一项业务                   │ ❌ 不落库        │
 ├─────────────────────────────┼──────────────────────────────────────┼──────────────────┤
 │ Knowledge space（知识空间） │ 存业务知识的根目录，微服务空间的容器 │ ❌ 不落库        │
 ├─────────────────────────────┼──────────────────────────────────────┼──────────────────┤
 │ Space（微服务空间）         │ 知识空间下的一个文件夹 = 一项微服务  │ ✅ space 表      │
 ├─────────────────────────────┼──────────────────────────────────────┼──────────────────┤
 │ UserSpace                   │ 谁能看哪个 Space                     │ ✅ user_space 表 │
 ├─────────────────────────────┼──────────────────────────────────────┼──────────────────┤
 │ Space path                  │ 相对项目根的路径                     │ ✅ space.path    │
 └─────────────────────────────┴──────────────────────────────────────┴──────────────────┘

 微服务与微服务空间一一对应（空间名即微服务名），所以不加表——按你的 Q4 答复。workspace 一律列进 _Avoid_。

 ### 改名对照

 ```
   MicroServiceSpace  → Space          micro_service_space → space
   UserWorkspace      → UserSpace      user_microservice   → user_space
   workspace_name     → space_name
 ```

 UserTables.workspaces → UserTables.spaces。全部调用方已跟改（repositories / routers / schemas / services / orm.py），旧名零残留。

 ### 顺手修的 4 个 bug

 1. list_by_user 恒返回空 —— 原来是 list(scalars().__dict__)，真实 ScalarResult.__dict__ 是 {}。改为 .scalars().all()。你之前 /user/spaces 一直报「用户有0」就是这个。
 2. spaces_users/create 非超管也能执行 —— if not user.is_super 只记日志、没 return。已改 403。
 3. 迁移会删 LangGraph 表 —— 那段 autogenerate 噪声不止会丢历史，全新库上直接报错（我实测：ProgrammingError: 索引 "checkpoint_writes_thread_id_idx" 不存在），因为那些表由
    checkpointer 启动时 .setup() 建。已删除。
 4. orm.py:270 循环导入 —— from custom.models import X 在「先 import custom」的顺序下炸。改为 import custom.models，两种导入顺序都通过。

 ### 验证

 - 迁移：全新库 upgrade head ✅；真库克隆（438 thread / 45 assistant）升级 ✅ 数据完好；downgrade -1 往返 ✅
 - 端到端：真起服务（2027 端口），注册 → 登录 → 403 鉴权 → 建空间 → 分配 → 列空间，数据正确落库 ✅
 - 回归：pytest tests/unit 改动前后完全一致（17 failed / 1880 passed，失败全是既有的、与我无关）✅

 ### 遗留

 - 迁移已在你的库里执行过，所以改名走的是新迁移 b4c1d2e3f4a5（rename_table），不是重建。跑 uv run alembic upgrade head 即可，注意用 uv run——直接 python -m alembic 会被仓库
   里的 alembic/ 目录遮蔽掉包。
 - 两处 raise HTTPException 换掉了原来的「记日志+返回消息」风格，如果你更想要软失败可以改回。
 - CreateSpaceResponse 里 space_name 是 str | None 但实际总有值，没动。

 ────────────────────────────────────────────────────────────────────────────────

 跳过没做的：没写 ADR（改名可逆、且不算「未来读者会疑惑」的决策）；没碰 KNOWLEDGE_SPACE_DIR 环境变量（OKF_DIR 那套还留在 graphs/，等你决定知识空间根怎么配）；没动
 space.path 的唯一约束。风险：space.path 现在只是普通字符串，谁都能写绝对路径或 ..——等做权限后端时必须校验，否则就是任意目录读取。