from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kinvest_trade.cli import main


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # 인자 없이 실행하면 기본값은 auto-run이다.
        # auto-run은 config의 auto_trade.symbol에 고정된 단일 종목만
        # 감시·매매한다(타겟 자동 선정 없음). 여러 종목 중 활발한
        # 종목을 자동으로 골라 매매하려면 'liquidity-lab'을 명시한다.
        sys.argv.append("auto-run")
    main()
