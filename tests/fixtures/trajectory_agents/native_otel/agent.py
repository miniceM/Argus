"""独立 native_otel 业务进程入口。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business import serve  # noqa: E402

if __name__ == "__main__":
    serve("native_otel")
