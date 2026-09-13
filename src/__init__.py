"""QQ群管理插件内部模块。

注意：除 main.py / web_api.py / commands.py 之外，本包内模块尽量不依赖
AstrBot 运行时，方便在纯 Python 环境下做单元测试。
"""

__all__ = [
    "api_client",
    "audit",
    "models",
    "scheduler",
    "store",
    "utils",
]
