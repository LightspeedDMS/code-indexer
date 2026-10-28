import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_ANALYSIS = PROJECT_ROOT / "scripts" / "analysis"
if str(SCRIPTS_ANALYSIS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ANALYSIS))
