"""把 3 个 space 的概念文档 frontmatter 升级到 OKF v0.2 生命周期字段。

v0.2 (SPEC §11): status ∈ {draft,stable,deprecated}、stale_after、
generated.by 必填、verified、sources。index.md 改为纯分组链接列表 +
okf_version 标记（另行手写），log.md 新建（另行手写）。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "knowledge"

# 各 space 概念文档的溯源与生成信息
META = {
    "hybrid-bonding": {
        "generated_by": "okf-enrichment-agent",
        "stale_after": "2027-04-20",
        "sources": [
            "https://doi.org/10.1109/ECTC.2019.00034  # 公开文献量级参照（合成数据）",
            "internal://demo-doe  # scripts/gen_okf_demo_data.py 生成的演示 DOE",
        ],
    },
    "cbam-reporting": {
        "generated_by": "okf-enrichment-agent",
        "stale_after": "2027-01-31",  # 规则随 Omnibus 修订，半年内必须复核
        "sources": [
            "https://eur-lex.europa.eu/eli/reg/2023/956  # CBAM 条例",
            "https://eur-lex.europa.eu/  # 2025 Omnibus 简化修订（公开文本）",
            "internal://demo-data  # 排放因子为合成演示数据",
        ],
    },
    "sbol3-synbio": {
        "generated_by": "okf-enrichment-agent",
        "stale_after": "2027-10-20",
        "sources": [
            "https://sbolstandard.org/datamodel-specification/  # SBOL3 数据模型规范",
            "https://github.com/SynBioDex/SBOLSpec  # SO 术语编号",
        ],
    },
}

V02_FIELDS = """status: {status}
verified: {verified}
generated:
  by: {by}
  at: 2026-10-20
stale_after: {stale_after}
sources:
{sources}"""

# 文档 -> (status, verified)；表格数据集所在 space 的索引页标 stable，其余按成熟度
STATUS = {
    "hybrid-bonding": {"index": ("stable", "true")},
    "cbam-reporting": {"index": ("stable", "false")},  # 演示因子表未与官方 AWG 核对
    "sbol3-synbio": {"index": ("stable", "true")},
}


def patch(path: Path, space: str) -> None:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, f"no frontmatter: {path}"
    fm = m.group(1)
    if "status:" in fm:
        return  # 已升级过
    meta = META[space]
    status, verified = STATUS[space].get(path.stem, ("draft", "false"))
    if path.stem != "index":
        status, verified = "stable", "true"
    if space == "cbam-reporting" and path.stem != "index":
        verified = "false"  # 规则文档需随 Omnibus 复核
    sources = "\n".join(f'  - "{s}"' for s in meta["sources"])
    fields = V02_FIELDS.format(
        status=status, verified=verified, by=meta["generated_by"],
        stale_after=meta["stale_after"], sources=sources,
    )
    new_fm = f"{fm}\n{fields}"
    path.write_text(f"---\n{new_fm}\n---\n{text[m.end():]}", encoding="utf-8")
    print("patched:", path.relative_to(ROOT))


for space in META:
    for md in sorted((ROOT / space).glob("*.md")):
        if md.stem == "log":
            continue
        patch(md, space)
