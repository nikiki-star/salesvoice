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
"""

collect_ignore = [
    "test_asr.py",
    "e2e_test.py",
    "compare_engines.py",
    "probe_langextract.py",
    "run_extract.py",
]
