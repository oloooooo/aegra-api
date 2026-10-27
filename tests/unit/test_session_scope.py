"""会话作用域：签发/验签/解析（ADR-0015 §4.1）。

锁两件事：**伪造的 `session_scope` 必须被拒**（客户端能往 configurable 里塞任意键），
以及**子 agent 不能借它拿到用户全部 space**（那是 `allowed_spaces` 的独立判定）。
"""

from graphs.access import session_scope as ss


def test_mint_verify_roundtrip():
    assert ss.verify(ss.mint("u1", "t-1")) == ("u1", "t-1")


def test_verify_rejects_tampered_owner():
    scope = ss.mint("u1", "t-1")
    assert ss.verify({**scope, "owner": "u2"}) is None


def test_verify_rejects_tampered_thread():
    scope = ss.mint("u1", "t-1")
    assert ss.verify({**scope, "thread_id": "t-2"}) is None


def test_verify_rejects_unsigned():
    assert ss.verify({"owner": "u1", "thread_id": "t-1"}) is None
    assert ss.verify({"owner": "u1", "thread_id": "t-1", "sig": "x" * 64}) is None
    assert ss.verify(None) is None
    assert ss.verify("nope") is None


def test_resolve_prefers_signed_scope():
    scope = ss.mint("u1", "t-1")
    cfg = {
        "configurable": {
            "langgraph_auth_user": {"identity": "subagent_service"},
            "session_scope": scope,
        }
    }
    assert ss.resolve(cfg) == ("u1", "t-1")


def test_resolve_fails_closed_when_scope_is_forged():
    """带了 session_scope 但验不过 → 当作无权，**不回退**到"用自身身份"。"""
    cfg = {
        "configurable": {
            "langgraph_auth_user": {"identity": "u1", "user_spaces": ["sp"]},
            "thread_id": "t-1",
            "session_scope": {"owner": "u2", "thread_id": "t-9", "sig": "bad"},
        }
    }
    assert ss.resolve(cfg) is None


def test_resolve_uses_own_identity_for_supervisor():
    cfg = {
        "configurable": {
            "langgraph_auth_user": {"identity": "u1", "user_spaces": ["sp"]},
            "thread_id": "t-1",
        }
    }
    assert ss.resolve(cfg) == ("u1", "t-1")


def test_resolve_without_auth_is_none():
    assert ss.resolve({"configurable": {"thread_id": "t-1"}}) is None
    assert ss.resolve({}) is None


def test_service_identity_without_scope_is_denied():
    """子 agent 没带转发凭证 → fail-closed，而不是落到 h(subagent_service) 的空命名空间。"""
    cfg = {
        "configurable": {
            "langgraph_auth_user": {"identity": "subagent_service"},
            "user_spaces": ["sp"],
            "thread_id": "child-1",
        }
    }
    assert ss.resolve(cfg) is None
