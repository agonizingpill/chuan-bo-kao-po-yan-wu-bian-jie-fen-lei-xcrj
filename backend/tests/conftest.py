import sys
from pathlib import Path

# The application package lives at backend/src; make it importable whether pytest
# is launched from the repository root or from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
