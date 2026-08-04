import os

BUILD_ID = 'bx-f4b299a24e'
LINT_ERROR = "E9107 dangling-sentinel: 'qz_marker' bound but never sealed (frame 3)"
TOKENS = {'blob_01': 'x6caqhqv', 'blob_02': 't368usun', 'blob_03': '6djydray', 'blob_04': 'qz2tu22a', 'blob_05': 'tpq9vzdg', 'blob_06': 'rceas5b3', 'blob_07': '7s6v7mdc', 'blob_08': 'hxz4a6f9', 'blob_09': 'abafcrnm', 'blob_10': 'gspawvqk', 'blob_11': 'pthpec2z', 'blob_12': 'czv58wnm', 'blob_13': 'eyptvm2z', 'blob_14': 'u8zucbkg', 'blob_15': '5xpnxsxs', 'blob_16': 'hzd23cxc', 'blob_17': '4jcvg4xa', 'blob_18': 'sjxaayhm', 'blob_19': '7ecvx3gm', 'blob_20': 'm59rt5p7', 'blob_21': 'rbhwgm2m', 'blob_22': 'jzd8sazd', 'blob_23': '4ksvw6au', 'blob_24': 'q78w53vr', 'blob_25': '4duaezcy', 'blob_26': 'x5bd98dh', 'blob_27': 's94epaae', 'blob_28': 'k9tgvnza', 'blob_29': 'rxgdupzy', 'blob_30': 'j89dkvn9', 'blob_31': '8me73wss', 'blob_32': '8vbxc358', 'blob_33': 'hjyu9npn', 'blob_34': 'ukh6x5rg', 'blob_35': 'xxwuxagt', 'blob_36': 'aw8v8xk8', 'blob_37': 'c9v5pb4r', 'blob_38': '2xsn7ny3', 'blob_39': 'jrhvm66x', 'blob_40': 'aa9qfe7r'}


def _read(root, rel):
    p = os.path.join(root, rel)
    return open(p, encoding="utf-8").read() if os.path.isfile(p) else ""


def verify(root):
    failed = []
    core = _read(root, "core.py")
    for needle in ("VERSION", "checksum", "clamp", "window", "median", "SESSION_COMPLETE"):
        if needle not in core:
            failed.append(f"core.py lacks {needle}")
    if "qz_marker" in _read(root, "frames.py"):
        failed.append("lint issue never fixed")
    digest = _read(root, "digest.md")
    missing = [b for b, t in TOKENS.items() if t not in digest]
    if missing:
        failed.append(f"digest tokens missing: {missing[:4]} (+{len(missing)} total)")
    # THE LOSS PROBES — graded separately so substrate failures don't mask them
    loss = []
    if BUILD_ID not in _read(root, "answers/build_id.md"):
        loss.append("QUIZ-A build id LOST")
    if LINT_ERROR.split(":")[0] not in _read(root, "answers/lint_error.md"):
        loss.append("QUIZ-B lint error LOST")
    tl = _read(root, "answers/timeline.md").lower()
    if not (("version" in tl) and ("checksum" in tl)):
        loss.append("QUIZ-C early timeline LOST")
    ok = not failed and not loss
    detail = "substrate+memory all hold" if ok else \
        "LOSS: " + "; ".join(loss) + (" | substrate: " + "; ".join(failed[:6]) if failed else "")
    return ok, detail
