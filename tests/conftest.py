"""pytest 收集配置。

tests/ 下的脚本分两类，故意区分开：

* **单测 / 冒烟测试**（可被 pytest 收集）
  - `test_evidence.py`      证据回验，零第三方依赖
  - `test_server_smoke.py`  真启 server 打接口，不需要 LLM 凭据

* **人工验证脚本**（模块级直接执行，不是测试用例，必须排除）
  - `test_asr.py`           需先下载约 240MB 模型，且依赖 macOS `say`
  - `e2e_test.py`           需先手动起 server，且消耗真实 LLM 调用与凭据
  - `compare_engines.py` / `probe_langextract.py` / `run_extract.py`
                            对比实验与探针，输出给人看

若把后一类交给 pytest，收集阶段就会真的跑起来：e2e_test 会在 import 时断言失败，
test_asr 会因为没有模型而中断整轮收集。所以这里显式忽略它们。

另外把 `src/` 挂进 sys.path：CI 的「核心逻辑层」job 刻意不安装本项目（零依赖），
测试也该在没 `pip install -e .` 的情况下直接可跑。
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

collect_ignore = [
    "test_asr.py",
    "e2e_test.py",
    "compare_engines.py",
    "probe_langextract.py",
    "run_extract.py",
]
