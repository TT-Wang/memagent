"""Reference solution (VALIDATION ONLY -- never shown to benchmarked agents).

Renames the single concept `rollout_percentage` -> `rollout_pct` everywhere it
appears across the package, the on-disk JSON config, and the demo, keeping
behavior byte-for-byte identical. The distractor names `bucket_percentage` and
`sample_percentage` do not contain the substring `rollout_percentage`, so a
literal token replacement leaves them untouched; the English word "percentage"
in prose is likewise untouched.
"""
import os


_OLD = "rollout_percentage"
_NEW = "rollout_pct"


def _rename_in_file(path):
    with open(path, "r") as f:
        text = f.read()
    if _OLD not in text:
        return
    text = text.replace(_OLD, _NEW)
    with open(path, "w") as f:
        f.write(text)


def apply(workdir):
    """Apply the rename to every relevant file in a freshly set-up workdir."""
    # All .py files anywhere under the workdir.
    for root, _dirs, files in os.walk(workdir):
        # skip cached bytecode dirs
        if os.path.basename(root) == "__pycache__":
            continue
        for fn in files:
            if fn.endswith(".py"):
                _rename_in_file(os.path.join(root, fn))

    # The on-disk JSON config key must move too.
    flags_json = os.path.join(workdir, "flags.json")
    if os.path.isfile(flags_json):
        _rename_in_file(flags_json)
