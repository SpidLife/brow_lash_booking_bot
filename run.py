import runpy
import sys
from pathlib import Path

folder = Path(__file__).parent / "brow_lash_booking_bot_v4"
sys.path.insert(0, str(folder))
runpy.run_path(str(folder / "run.py"), run_name="__main__")
