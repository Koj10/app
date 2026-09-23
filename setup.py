from cx_Freeze import setup, Executable
import sys
import re
import os
import glob


def get_version_from_file(file_path: str, version_var: str = "VERSION"):
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    pattern = rf"^{version_var}\s*=\s*['\"](.*?)['\"]|{version_var}\s*=\s*([\d.]+)"
    match = re.search(pattern, content, re.MULTILINE)
    if match:
        return match.group(1) or match.group(2)
    return None


def _collect_runtime_dlls():
    """VC++ runtime и python3*.dll — без них на чистом Windows будет ошибка VCRUNTIME140.dll."""
    names = {
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "msvcp140.dll",
    }
    py_dll = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    names.add(py_dll.lower())

    roots = [
        sys.base_prefix,
        os.path.join(sys.base_prefix, "DLLs"),
        os.path.dirname(sys.executable),
    ]
    if os.environ.get("SystemRoot"):
        roots.append(os.path.join(os.environ["SystemRoot"], "System32"))

    found = []
    seen = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for name in names:
            if name in seen:
                continue
            path = os.path.join(root, name)
            if os.path.isfile(path):
                found.append(path)
                seen.add(name)

    missing = names - seen
    if missing:
        print(f"WARN: не найдены runtime DLL: {', '.join(sorted(missing))}")
    return found


version = get_version_from_file("app.py")

base = "gui" if sys.platform == "win32" else None

if sys.platform == "win32":
    temp_dir = os.path.abspath(os.path.join("build", "tmp"))
    os.makedirs(temp_dir, exist_ok=True)
    os.environ["TEMP"] = temp_dir
    os.environ["TMP"] = temp_dir

icon_file = "logo.ico"

runtime_dlls = _collect_runtime_dlls()

build_exe_options = {
    "include_files": [icon_file] + runtime_dlls,
    "packages": [
        "win32api",
        "win32con",
        "win32gui",
        "win32com",
        "win32com.client",
    ],
}

msi_options = {
    "upgrade_code": "{F7A2E5C3-9D4E-4A8C-9B8D-7A1234567890}",
    "add_to_path": False,
    "initial_target_dir": r"[ProgramFilesFolder]\GameSense",
}

setup(
    name="GameSense",
    version=version,
    description="GameSense Software",
    author="falbue",
    author_email="cyansair05@gmail.com",
    options={
        "build_exe": build_exe_options,
        "bdist_msi": msi_options,
    },
    executables=[
        Executable(
            script="app.py",
            base=base,
            icon=icon_file,
            target_name="GameSense.exe",
            shortcut_name="GameSense",
            shortcut_dir="DesktopFolder",
        )
    ],
)
