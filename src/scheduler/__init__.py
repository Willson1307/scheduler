"""日程调度辅助系统 · 数据层（B0 批次）。

本包只依赖 Python 标准库，不绑定任何 Web 框架——
目的是让后续无论选 FastAPI / Flask 还是别的形态，都能直接复用。
"""

from . import db, object_graph, occupancy, schedules, slots  # noqa: F401

__all__ = ["db", "object_graph", "occupancy", "schedules", "slots"]
__version__ = "0.1.0-b0"
