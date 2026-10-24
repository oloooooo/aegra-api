"""`custom/outbox.py` 文本口径的回归测试。

取消通知里那句“别自己重派”是有来历的：主管被别的任务唤醒时看到待办还差一项，
会把 `was cancelled` 当成“这轮失败”而自动重新派发（2026-10-24 实测，取消后 3 秒
又派了同名 hybrid-bonding 任务）。
"""

from custom.outbox import PREFIX, completion_text, notice_text


def test_cancelled_notice_tells_supervisor_not_to_redispatch() -> None:
    text = notice_text("task-1", "run-1", "domain_agent", "cancelled")
    assert text.startswith(f"{PREFIX} 'domain_agent' was cancelled]")  # 前端靠这句渲染灰条
    assert "Do not re-dispatch" in text
    assert "Result:" not in text  # 有它前端就会渲染成结果卡片
    assert "task_id: task-1" in text and "run_id: run-1" in text


def test_interrupted_notice_stays_resumable() -> None:
    text = notice_text("task-2", "run-2", "domain_agent", "interrupted")
    assert "was interrupted" in text
    assert "Do not re-dispatch" not in text  # HITL 中断现场还在，不该禁止续跑


def test_success_result_text_unchanged() -> None:
    text = completion_text("task-3", "run-3", "domain_agent", "success", "结论")
    assert text == f"{PREFIX} 'domain_agent' has completed] Result: 结论\n\ntask_id: task-3\nrun_id: run-3"
