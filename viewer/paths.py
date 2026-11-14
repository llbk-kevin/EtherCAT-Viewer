import os
from pathlib import Path
import sys


def data_root():
    if getattr(sys, "frozen", False):
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return local / "EtherCAT-Viewer"
    return Path(__file__).resolve().parent.parent


def powershell_path():
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return str(system / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
