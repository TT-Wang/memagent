import os

MODULES = ['core', 'worker', 'audit', 'gateway']


def setup(root):
    for m in MODULES:
        with open(os.path.join(root, m + ".py"), "w", encoding="utf-8") as f:
            f.write("REGISTRY = {\n}\n")
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write("# policy flags\nFour flag registries, one per module. REGISTRY maps "
                "flag name -> default.\n")
