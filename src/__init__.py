"""McPair · 麦麦配餐师"""

import sys

__version__ = "0.1.0"

# 本包用了 @dataclass (3.7+) 与 from __future__ import annotations (3.7+)，
# 并在类型注解里使用 list[str] / X | None（3.9+ 才在运行时安全）。
# 这里做前置检查：否则 3.6 只会抛一句难以理解的 SyntaxError。
_MIN_VERSION = (3, 9)

if sys.version_info < _MIN_VERSION:
    _cur = "{0}.{1}.{2}".format(*sys.version_info[:3])
    _need = "{0}.{1}".format(*_MIN_VERSION)
    raise SystemExit(
        "\n"
        + "=" * 64
        + "\nMcPair 需要 Python "
        + _need
        + " 及以上，当前解释器是 "
        + _cur
        + "\n\n"
        + "这通常是因为 Anaconda base 环境版本较老。项目本身零依赖，\n"
        + "不需要 pip install，换一个解释器即可：\n\n"
        + "  conda activate py309\n"
        + "  python -m src.cli --budget 40\n\n"
        + "或者直接用绝对路径：\n\n"
        + "  D:\\anaconda\\envs\\py309\\python.exe -m src.cli --budget 40\n"
        + "=" * 64
        + "\n"
    )
