import os


def setup(root):
    os.makedirs(os.path.join(root, "mathlib"), exist_ok=True)
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write("# mathlib\nSmall numeric helpers. mathlib/seq.py holds sequence helpers used "
                "across the package; stats/report build on them.\n")
    with open(os.path.join(root, "mathlib", "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")
    with open(os.path.join(root, "mathlib", "seq.py"), "w", encoding="utf-8") as f:
        f.write(
            "def normalize(seq):\n"
            "    out = []\n"
            "    for v in sorted(seq):\n"
            "        if not out or out[-1] != v:\n"
            "            out.append(v)\n"
            "    return out\n"
            "\n"
            "\n"
            "def window(seq, n):\n"
            "    items = list(seq)\n"
            "    return [tuple(items[i:i + n]) for i in range(len(items) - n + 1)]\n"
            "\n"
            "\n"
            "def scale(seq, k):\n"
            "    return [v * k for v in seq]\n")
    with open(os.path.join(root, "mathlib", "stats.py"), "w", encoding="utf-8") as f:
        f.write(
            "from .seq import normalize\n"
            "\n"
            "\n"
            "def summary(seq):\n"
            "    vals = normalize(seq)\n"
            "    # relies on normalize returning a NEW ascending list\n"
            "    return {\"mean\": sum(seq) / len(list(seq)), \"lo\": vals[0], \"hi\": vals[-1]}\n")
    with open(os.path.join(root, "mathlib", "report.py"), "w", encoding="utf-8") as f:
        f.write(
            "from .stats import summary\n"
            "\n"
            "\n"
            "def render(seq):\n"
            "    s = summary(seq)\n"
            "    mean = int(s[\"mean\"]) if float(s[\"mean\"]).is_integer() else s[\"mean\"]\n"
            "    return f\"mean={mean} range={s['lo']}..{s['hi']}\"\n")
