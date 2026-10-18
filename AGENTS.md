# agent运行要求
1. 在进行长时任务时，先使用plan规划任务，并列出任务，完成某一任务的时候要生成简报，告知用户
2. 环境需要安装新的依赖的时候，使用ask-user-question插件询问用户
3. 遇到较新的，没遇见的问题或者技术的时候，使用联网搜索功能获取最新信息
4. 目前请尽量在custom，graphs中修改或者添加代码，其他部分先不改，除非用户要求进行更改或者使用ask_user_question征询用户许可后改动

## Agent skills

### Issue tracker

Issue 与 spec 都放在 GitHub Issues，统一用 `gh` CLI 操作。见 `docs/agents/issue-tracker.md`。

### Triage labels

沿用默认五角色词表（`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`），标签字符串与角色同名。见 `docs/agents/triage-labels.md`。

### Domain docs

单上下文（single-context）：根 `CONTEXT.md` + `docs/adr/`。见 `docs/agents/domain.md`。