"""讓跨專案 contract tests 載入 sibling Retrieval Service 的 production package。"""

from __future__ import annotations

import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
RETRIEVAL_SOURCE = WORKSPACE_ROOT / "retrieval-service" / "src"

if str(RETRIEVAL_SOURCE) not in sys.path:
    sys.path.insert(0, str(RETRIEVAL_SOURCE))
