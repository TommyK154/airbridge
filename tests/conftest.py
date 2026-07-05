"""Make the project root importable (main.py and tray.py are top-level files)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
