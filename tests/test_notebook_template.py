"""M3-7 notebook 模板：JSON 结构合法、代码单元可编译、README 说明齐全。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks" / "explore.ipynb"
README = ROOT / "notebooks" / "README.md"


def test_notebook_code_cells_compile() -> None:
    data = json.loads(NOTEBOOK.read_text(encoding="utf-8"))

    assert data["nbformat"] == 4
    code_cells = [
        cell for cell in data["cells"] if cell["cell_type"] == "code"
    ]
    assert code_cells
    for cell in code_cells:
        source = "".join(cell["source"])
        # 手写 ipynb 容易漏换行/引号，编译一遍能挡住低级语法错误
        compile(source, "<notebook>", "exec")


def test_notebook_only_reads_export_dir() -> None:
    """原则：notebook 不直连 DB/Redis/OKX（只读导出目录）。"""
    data = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    source = "\n".join(
        "".join(cell["source"])
        for cell in data["cells"]
        if cell["cell_type"] == "code"
    )

    for forbidden in ("sqlite3", "redis", "okx"):
        assert forbidden not in source.lower()


def test_readme_documents_export_and_env() -> None:
    text = README.read_text(encoding="utf-8")

    assert "quant research export" in text
    assert "QUANT_RESEARCH_DIR" in text
    assert "backtest.json" in text
