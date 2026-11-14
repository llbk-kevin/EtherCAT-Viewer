from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import subprocess
import sys

def meets_requirements(requirements):
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        package, expected = line.split("==", 1)
        try:
            if version(package) != expected:
                return False
        except PackageNotFoundError:
            return False
    return True

def main():
    requirements = Path(__file__).resolve().parent / "requirements.txt"
    if not meets_requirements(requirements):
        subprocess.run([sys.executable, "-m", "pip", "install", "--only-binary=:all:",
                        "-r", str(requirements)], check=True)
    import uvicorn
    uvicorn.run("viewer.server:app", host="127.0.0.1", port=8765)

if __name__ == "__main__":
    main()
