import os


def setup(root):
    os.makedirs(os.path.join(root, "taskdag"), exist_ok=True)
    os.makedirs(os.path.join(root, "tests"), exist_ok=True)
    with open(os.path.join(root, "taskdag", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("__version__ = '0.1.0'\n")
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write("# taskdag\nA tiny task DAG toolkit (session-built).\n")
