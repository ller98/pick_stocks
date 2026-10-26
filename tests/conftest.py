import os
import sys

# 让 pytest 能 import picker 包（项目根加入 sys.path）
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
