"""Execute every notebook in notebooks/ in place with nbclient, so saved outputs are real."""
from pathlib import Path

import nbformat
from nbclient import NotebookClient

ROOT = Path(__file__).resolve().parents[1]

for path in sorted((ROOT / "notebooks").glob("*.ipynb")):
    nb = nbformat.read(path, as_version=4)
    NotebookClient(nb, timeout=600, kernel_name="python3",
                   resources={"metadata": {"path": str(ROOT / "notebooks")}}).execute()
    nbformat.write(nb, path)
    print(f"executed {path.name}")
