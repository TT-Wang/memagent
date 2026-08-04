import os

# Correct final module after ALL 50 turns — rendered by _gen_long50.py from the same op replay
# that produced prompts.json and verify.py. VALIDATION ONLY.
FINAL = '"""Feature-flag registry for the app.\n\nConventions (KEEP THESE — reviewers reject violations):\n  * flag names are kebab-case strings\n  * each flag is a dict with exactly two fields: "default" (bool) and "owner" (str team name)\n  * retiring a flag MOVES its entry (unchanged) from FLAGS to RETIRED — never delete history\n"""\n\nFLAGS = {\n    "avatars-next": {"default": True, "owner": "web"},\n    "billing-next-next-v2": {"default": False, "owner": "payments"},\n    "dashboards": {"default": False, "owner": "web"},\n    "digest": {"default": False, "owner": "growth"},\n    "exports": {"default": False, "owner": "infra"},\n    "labels": {"default": False, "owner": "search"},\n    "new-checkout": {"default": False, "owner": "infra"},\n    "previews": {"default": True, "owner": "mobile"},\n    "themes": {"default": False, "owner": "data"},\n    "webhooks": {"default": False, "owner": "search"},\n}\n\nRETIRED = {\n    "beta-search": {"default": False, "owner": "platform"},\n    "dark-mode": {"default": False, "owner": "web"},\n    "imports-v2": {"default": True, "owner": "mobile"},\n    "invites": {"default": False, "owner": "growth"},\n}\n\n\ndef is_enabled(name: str) -> bool:\n    return bool(FLAGS.get(name, {}).get("default", False))\n'


def apply(workdir):
    with open(os.path.join(workdir, 'flags/registry.py'), "w", encoding="utf-8") as f:
        f.write(FINAL)
