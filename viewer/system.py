"""当前用户的登录启动项；不安装服务或写入管理员级位置。"""
from pathlib import Path
import subprocess
import sys

NAME = "EtherCATViewer"
KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command():
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline([sys.executable, "--autostart"])
    return subprocess.list2cmdline([sys.executable, str(Path(__file__).resolve().parent.parent/"launch.py"), "--autostart"])


def login_status():
    if sys.platform != "win32":
        return dict(enabled=False, supported=False)
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            value, _ = winreg.QueryValueEx(key, NAME)
        return dict(enabled=True, supported=True, command=value, current=value == startup_command())
    except FileNotFoundError:
        return dict(enabled=False, supported=True, current=True)


def set_login(enabled):
    if sys.platform != "win32":
        raise ValueError("登录启动仅支持 Windows")
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as key:
        if enabled:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, NAME)
            except FileNotFoundError:
                pass
    return login_status()
