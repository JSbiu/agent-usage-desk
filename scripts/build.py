"""Offline Windows package: stdlib-only Python runtime + hidden .NET launcher."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def extension_id(manifest):
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    return "".join(chr(97 + int(c, 16)) for c in digest)


def build():
    if sys.platform != "win32":
        raise SystemExit("此打包脚本使用 Windows 的 .NET Framework 编译器。")
    package = ROOT / "dist" / "AgentUsageDesk"
    runtime = package / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    prefix = Path(sys.base_prefix)
    version = str(sys.version_info.major) + str(sys.version_info.minor)
    for name in ("python.exe", "python" + version + ".dll", "python3.dll", "LICENSE.txt"):
        shutil.copy2(prefix / name, runtime / name)
    for path in prefix.glob("vcruntime*.dll"):
        shutil.copy2(path, runtime / path.name)
    dlls = runtime / "DLLs"
    dlls.mkdir(exist_ok=True)
    for path in (prefix / "DLLs").iterdir():
        if path.suffix.lower() in (".pyd", ".dll") and not any(s in path.name.lower() for s in ("tk", "tcl", "_test")):
            shutil.copy2(path, dlls / path.name)
    excluded = {"site-packages", "__pycache__", "test", "tests", "tkinter", "idlelib", "turtledemo", "ensurepip", "venv"}
    with zipfile.ZipFile(runtime / "stdlib.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in (prefix / "Lib").rglob("*.py"):
            relative = path.relative_to(prefix / "Lib")
            if not excluded.intersection(relative.parts):
                archive.write(path, str(relative))
    (runtime / ("python" + version + "._pth")).write_text("stdlib.zip\nDLLs\n.\n../native\n", encoding="utf-8")
    shutil.copytree(ROOT / "extension", package / "extension", dirs_exist_ok=True)
    shutil.copytree(ROOT / "native", package / "native", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "docs", package / "docs", dirs_exist_ok=True)
    for name in ("install.ps1", "uninstall.ps1"):
        shutil.copy2(ROOT / "scripts" / name, package / name)
    if (ROOT / "README.md").exists():
        shutil.copy2(ROOT / "README.md", package / "README.md")
    shutil.copy2(ROOT / "LICENSE", package / "LICENSE")
    shutil.copy2(ROOT / "docs" / "third-party.md", package / "THIRD-PARTY.md")
    compiler = Path("C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe")
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/optimize+", "/out:" + str(package / "CodexMonitorHost.exe"), str(ROOT / "launcher" / "Launcher.cs")], check=True)
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
    identity = extension_id(manifest)
    (package / "extension-id.txt").write_text(identity + "\n", encoding="utf-8")
    archive_path = ROOT / "dist" / ("AgentUsageDesk-" + manifest["version"] + "-windows.zip")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                archive.write(path, str(path.relative_to(package.parent)))
    print("Package:", package)
    print("ZIP:", archive_path)
    print("Extension ID:", identity)
    print("ZIP size:", round(archive_path.stat().st_size / 1024 / 1024, 1), "MB")


if __name__ == "__main__":
    build()
