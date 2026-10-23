"""rnbatch — 批量重命名：关键词一致化。

从「基准文件」里读出项目名/人名/公司简称，把一堆混合格式文件按统一模板改名，
预览先行，随时可撤销。
"""

__version__ = "0.1.0"

from .engine import apply_plan, collect, plan, undo  # noqa: F401
from .extract import extract_text  # noqa: F401

__all__ = ["__version__", "collect", "plan", "apply_plan", "undo", "extract_text"]
