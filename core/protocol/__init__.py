"""协议转换层:各家 API 格式之间的纯函数翻译。

这里的东西**不许** import server / 数据库 / FastAPI —— 保持可独立单测。
按对端协议拆文件:anthropic.py / responses.py。
"""
