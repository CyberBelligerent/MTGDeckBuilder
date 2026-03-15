import subprocess
import sys

subprocess.run([
    sys.executable, "-m", "PyInstaller",
    "gui.py",
    "--name", "MTGDeckBuilder",
    "--onefile",
    "--windowed",                        # no console window on double-click
    "--collect-submodules", "sklearn",
    "--hidden-import", "sklearn.ensemble._forest",
    "--hidden-import", "sklearn.utils._typedefs",
    "--hidden-import", "sklearn.utils._heap",
    "--hidden-import", "sklearn.utils._sorting",
    "--hidden-import", "sklearn.neighbors._partition_nodes",
    "--hidden-import", "joblib",
    "--hidden-import", "tqdm",
    "--hidden-import", "bs4",
], check=True)

print("\nBuild complete.  Executable is in the dist/ folder.")
