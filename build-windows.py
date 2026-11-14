"""Build the Windows x64 release and write its checksum."""
from hashlib import file_digest
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
VERSION = "v0.1.0"
ASSET = f"EtherCAT-Viewer-{VERSION}-windows-x64.exe"


def main():
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32:
        raise SystemExit("构建需要 Windows 和 Python 3.12 x64。")

    env = ROOT / ".tooling" / "package-venv"
    python = env / "Scripts" / "python.exe"
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(env)], cwd=ROOT, check=True)
    subprocess.run([str(python), "-m", "pip", "install", "--only-binary=:all:",
                    "-r", "requirements.txt", "pyinstaller==6.22.3"], cwd=ROOT, check=True)
    subprocess.run([str(python), "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--workpath", "build", "--distpath", "dist", "EtherCAT-Viewer.spec"],
                   cwd=ROOT, check=True)

    asset = ROOT / "dist" / ASSET
    with asset.open("rb") as stream:
        sha256 = file_digest(stream, "sha256").hexdigest()
    asset.with_suffix(".exe.sha256").write_text(f"{sha256}  {asset.name}\n", encoding="ascii")
    print(asset)
    print(f"SHA-256: {sha256}")


if __name__ == "__main__":
    main()
