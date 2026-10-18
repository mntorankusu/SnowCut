#!/usr/bin/env python3
"""Build script for packaging SnowCut with PyInstaller.

Usage:
    python build.py               # Build onedir distribution in dist/SnowCut/
    python build.py --onefile     # Build single-file executable in dist/SnowCut.exe
    python build.py --clean       # Remove previous build artifacts before building
    python build.py --zip         # Package dist/SnowCut into dist/SnowCut-win64.zip
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_FILE = os.path.join(HERE, "SnowCut.spec")
BUILD_DIR = os.path.join(HERE, "build")
DIST_DIR = os.path.join(HERE, "dist")


def clean():
    print("[*] Cleaning build artifacts...")
    for p in (BUILD_DIR, DIST_DIR):
        if os.path.isdir(p):
            try:
                shutil.rmtree(p)
                print(f"    Removed: {p}")
            except Exception as e:
                print(f"    Warning: could not remove {p}: {e}")


def run_pyinstaller(onefile=False, clean_first=False):
    if clean_first:
        clean()

    env = os.environ.copy()
    if onefile:
        env["SNOWCUT_ONEFILE"] = "1"
        mode_str = "single-file executable (--onefile)"
    else:
        env["SNOWCUT_ONEFILE"] = "0"
        mode_str = "directory distribution (--onedir)"

    print(f"[*] Building SnowCut with PyInstaller ({mode_str})...")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        SPEC_FILE,
    ]

    result = subprocess.run(cmd, cwd=HERE, env=env)
    if result.returncode != 0:
        print(f"[!] PyInstaller build failed with exit code {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)

    if onefile:
        target = os.path.join(DIST_DIR, "SnowCut.exe")
        if os.path.exists(target):
            size_mb = os.path.getsize(target) / (1024 * 1024)
            print(f"[+] Build successful: {target} ({size_mb:.1f} MB)")
    else:
        target = os.path.join(DIST_DIR, "SnowCut", "SnowCut.exe")
        if os.path.exists(target):
            print(f"[+] Build successful: {os.path.dirname(target)}")


def make_zip():
    folder = os.path.join(DIST_DIR, "SnowCut")
    if not os.path.isdir(folder):
        print(f"[!] Cannot create zip: {folder} does not exist")
        return None

    zip_path = os.path.join(DIST_DIR, "SnowCut-win64.zip")
    print(f"[*] Compressing {folder} -> {zip_path}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(folder):
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, DIST_DIR)
                zf.write(full_path, rel_path)

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"[+] Created release zip: {zip_path} ({size_mb:.1f} MB)")
    return zip_path


def main():
    parser = argparse.ArgumentParser(description="Package SnowCut with PyInstaller")
    parser.add_argument("--onefile", action="store_true", help="Build a standalone single-file EXE")
    parser.add_argument("--onedir", action="store_true", help="Build a directory distribution (default)")
    parser.add_argument("--clean", action="store_true", help="Clean build directories before running")
    parser.add_argument("--zip", action="store_true", help="Create a distribution zip of the onedir build")

    args = parser.parse_args()

    onefile = args.onefile and not args.onedir
    run_pyinstaller(onefile=onefile, clean_first=args.clean)

    if args.zip and not onefile:
        make_zip()


if __name__ == "__main__":
    main()
