"""rnbatch — 批量重命名：关键词一致化。

从「基准文件」里读出项目名/人名/公司简称，把一堆混合格式文件按统一模板改名，
预览先行，随时可撤销。

三种用法：
  rnbatch <命令>      命令行
  rnbatch serve       本地 Web 应用（文件不出本机）
  rnbatch mcp         以 MCP server 运行，供任意 agent 调用
"""

__version__ = "0.2.1"

from .engine import (ConflictError, apply_plan, check_conflicts, collect,  # noqa: F401
                     plan, undo)
from .extract import extract_text  # noqa: F401
from .keywords import suggest  # noqa: F401

__all__ = ["__version__", "collect", "plan", "apply_plan", "undo",
           "check_conflicts", "ConflictError", "extract_text", "suggest"]
