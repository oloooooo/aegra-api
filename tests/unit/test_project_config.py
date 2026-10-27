"""config.yaml 的读取与优先级（custom/config.py）。

锁三件事：优先级（env > 文件 > 默认值）、类型写错要报错、仓库里那份 config.yaml 的键
必须都认识——否则改了文件却没人读，纯靠肉眼发现不了。
"""

from __future__ import annotations

import pytest

from shared import config as pc

_ENV = {spec[0] for spec in pc._SPEC.values()}


def test_defaults_when_nothing_configured(monkeypatch):
    monkeypatch.setattr(pc, "_FILE", {})
    for env in _ENV:
        monkeypatch.delenv(env, raising=False)
    for key, (_env_name, default) in pc._SPEC.items():
        assert pc.get(key) == default, key


def test_file_overrides_default(monkeypatch):
    monkeypatch.delenv("MAX_CELLS", raising=False)
    monkeypatch.setattr(pc, "_FILE", {"xlsx": {"max_cells": 5}})
    assert pc.get("xlsx.max_cells") == 5


def test_env_overrides_file(monkeypatch):
    monkeypatch.setattr(pc, "_FILE", {"xlsx": {"max_cells": 5}})
    monkeypatch.setenv("MAX_CELLS", "7")
    assert pc.get("xlsx.max_cells") == 7


def test_env_numeric_string_is_coerced(monkeypatch):
    monkeypatch.setattr(pc, "_FILE", {})
    monkeypatch.setenv("XLSX_QUERY_BUDGET_MS", "123")
    assert pc.get("xlsx.query_budget_ms") == 123


def test_empty_env_means_unset(monkeypatch):
    monkeypatch.setattr(pc, "_FILE", {"uploads": {"dir": "shared/uploads"}})
    monkeypatch.setenv("UPLOADS_DIR", "")
    assert pc.get("uploads.dir") == "shared/uploads"


def test_bad_value_raises_instead_of_silent_default(monkeypatch):
    monkeypatch.setattr(pc, "_FILE", {})
    monkeypatch.setenv("MAX_CELLS", "abc")
    with pytest.raises(ValueError, match="xlsx.max_cells"):
        pc.get("xlsx.max_cells")


def test_repo_config_file_only_uses_known_keys():
    data = pc._read_file()
    flat = {f"{section}.{key}" for section, body in data.items() for key in body}
    assert flat <= set(pc._SPEC), f"config.yaml 里有拼错或没接线的键: {sorted(flat - set(pc._SPEC))}"


def test_repo_config_file_values_are_typed(monkeypatch):
    """仓库里那份 config.yaml 逐键取一遍：类型不对会在这里炸，而不是等运行时。"""
    for env in _ENV:
        monkeypatch.delenv(env, raising=False)
    for key, (_env_name, default) in pc._SPEC.items():
        value = pc.get(key)
        if default is None:
            assert value is None or isinstance(value, str)
        else:
            assert isinstance(value, type(default)), f"{key}: {value!r}"
