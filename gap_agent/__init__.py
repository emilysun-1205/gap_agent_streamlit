"""Literature Gap & Hypothesis Discovery Agent - MVP 包。

架构(依据框架文档 §2):3 个核心 Agent + 5 个支撑模块
- Node 0 planner   :研究问题规划器
- Module 1 retriever:文献检索器
- Module 2 evaluator:质量与相关性评估
- Node 1 extractor :结构化文献抽取
- Module 3 store   :证据库与跨文献矩阵
- Node 2 gap_hunter:科研空白发现
- Node 3 hypothesis:科学假设架构师
- Module 4 validator:引用核验与假设验证
- Module 5 reporter :报告与数据导出
"""
__version__ = "0.1.0-mvp"
