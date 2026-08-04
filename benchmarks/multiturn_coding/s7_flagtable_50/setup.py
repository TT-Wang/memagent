import os

SEED = '"""Feature-flag registry for the app.\n\nConventions (KEEP THESE — reviewers reject violations):\n  * flag names are kebab-case strings\n  * each flag is a dict with exactly two fields: "default" (bool) and "owner" (str team name)\n  * retiring a flag MOVES its entry (unchanged) from FLAGS to RETIRED — never delete history\n"""\n\nFLAGS = {\n    "dark-mode": {"default": False, "owner": "web"},\n    "new-checkout": {"default": False, "owner": "payments"},\n    "beta-search": {"default": True, "owner": "search"},\n}\n\nRETIRED = {}\n\n\ndef is_enabled(name: str) -> bool:\n    return bool(FLAGS.get(name, {}).get("default", False))\n'


def setup(workdir):
    pkg = os.path.join(workdir, 'flags')
    os.makedirs(pkg, exist_ok=True)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    with open(os.path.join(workdir, 'flags/registry.py'), "w", encoding="utf-8") as f:
        f.write(SEED)
