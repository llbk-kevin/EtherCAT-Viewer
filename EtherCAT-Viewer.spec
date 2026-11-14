from importlib.metadata import distributions
from pathlib import Path
import sys

root = Path(SPECPATH)
notices = [(str(file), "licenses/SOEM") for file in (root / "licenses").glob("*.txt")]
notices.append((str(Path(sys.base_prefix) / "LICENSE.txt"), "licenses/Python"))
for distribution in distributions():
    for entry in distribution.files or ():
        if entry.name.lower().startswith(("license", "copying", "notice")):
            source = distribution.locate_file(entry)
            if source.is_file():
                notices.append((str(source), f"licenses/{distribution.metadata['Name']}"))

analysis = Analysis(
    [str(root / "launch.py")],
    pathex=[str(root)],
    datas=[(str(root / "viewer" / "static"), "viewer/static"),
           (str(root / "THIRD-PARTY-NOTICES.md"), "."), *notices],
    hiddenimports=["viewer.server", "uvicorn.lifespan.on", "uvicorn.loops.auto",
                   "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets.auto"],
)
# Use the Npcap DLLs installed by the operator. This also removes imports
# discovered recursively from the native PySOEM extension on the build host.
npcap_files = {"wpcap.dll", "packet.dll"}
analysis.binaries = [
    entry for entry in analysis.binaries
    if Path(entry[0]).name.lower() not in npcap_files and Path(entry[1]).name.lower() not in npcap_files
]
exe = EXE(
    PYZ(analysis.pure), analysis.scripts, analysis.binaries, analysis.datas, [],
    name="EtherCAT-Viewer-v0.1.0-windows-x64", console=True, upx=False,
)
