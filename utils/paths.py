import os
import sys

def get_bundle_dir() -> str:
    """
    Returns the base directory for bundled read-only assets (web/, migrations/).
    In PyInstaller frozen mode, this points to sys._MEIPASS.
    In development mode, this points to the project root directory.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return getattr(sys, "_MEIPASS")
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def get_app_dir() -> str:
    """
    Returns the persistent application directory where the .exe or project lives.
    Used for persistent, user-modifiable data (.env, data/drmt.db, data/logs/, static/*.csv).
    In PyInstaller frozen mode, this points to the folder containing the executable.
    In development mode, this points to the project root directory.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
