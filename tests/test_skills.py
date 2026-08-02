"""Skill frontmatter rules + project trust gate (ported from Pi's skills.ts / project-trust.ts).

No model, no pytest. Run: PYTHONPATH=src python tests/test_skills.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sliceagent.skills import (SKILL_DESCRIPTION_MAX, SKILL_NAME_MAX, SkillManager)  # noqa: E402

CHECKS = []
def check(fn):
    CHECKS.append(fn)
    return fn


def _write(root: str, rel: str, text: str) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


@check
def frontmatter_rules_bound_name_and_description():
    """Pi's frontmatter rules: name <= 64 chars (an identity token), description <= 1024 (the
    catalog is progressive disclosure, not a doc)."""
    root = tempfile.mkdtemp(prefix="skills-")
    _write(root, "longname/SKILL.md",
           "---\nname: " + "x" * (SKILL_NAME_MAX + 1) + "\ndescription: ok\n---\nBody.\n")
    _write(root, "longdesc/SKILL.md",
           "---\nname: ok-name\ndescription: " + "d" * (SKILL_DESCRIPTION_MAX + 50) + "\n---\nBody.\n")
    logs = []
    manager = SkillManager([root], on_log=logs.append)
    assert manager.get("x" * (SKILL_NAME_MAX + 1)) is None, "an over-long name must be skipped"
    assert any("name >" in line for line in logs), logs
    skill = manager.get("ok-name")
    assert skill is not None and len(skill.description) <= SKILL_DESCRIPTION_MAX
    assert skill.description.endswith("…")


@check
def disable_model_invocation_hides_from_the_tool_catalog_but_stays_user_addressable():
    """Pi's `disable-model-invocation` flag: the skill never reaches the model's tool catalog,
    but stays on disk and visible (marked) in /skills."""
    root = tempfile.mkdtemp(prefix="skills-")
    _write(root, "manual/SKILL.md",
           "---\nname: manual-only\ndescription: m\ndisable-model-invocation: true\n---\nBody.\n")
    _write(root, "auto/SKILL.md", "---\nname: auto-one\ndescription: a\n---\nBody.\n")
    manager = SkillManager([root])
    assert [n for n, _ in manager.catalog()] == ["auto-one"], manager.catalog()
    assert [n for n, _ in manager.catalog(model_only=False)] == ["auto-one", "manual-only"]
    assert manager.get("manual-only") is not None and manager.load("manual-only") == "Body."


@check
def project_skills_load_only_under_trust():
    """Pi's project-trust gate: repo-local skills are repo-controlled instructions — they never
    load by mere presence. A `.sliceagent/skills-trust` marker or AGENT_PROJECT_SKILLS=1 admits
    them; user/global roots are trusted by definition."""
    workspace = tempfile.mkdtemp(prefix="ws-")
    project_root = os.path.join(workspace, ".sliceagent", "skills")
    user_root = tempfile.mkdtemp(prefix="user-skills-")
    _write(project_root, "evil/SKILL.md", "---\nname: repo-skill\ndescription: r\n---\nRepo.\n")
    _write(user_root, "mine/SKILL.md", "---\nname: user-skill\ndescription: u\n---\nMine.\n")
    logs = []

    untrusted = SkillManager([project_root, user_root], project_root=workspace,
                             trust_project=False, on_log=logs.append)
    assert untrusted.get("repo-skill") is None, "an untrusted repo's skill must not load"
    assert untrusted.get("user-skill") is not None, "the user root is trusted by definition"
    assert any("untrusted workspace" in line for line in logs), logs

    # the marker file admits the workspace
    os.makedirs(os.path.join(workspace, ".sliceagent"), exist_ok=True)
    open(os.path.join(workspace, ".sliceagent", "skills-trust"), "w", encoding="utf-8").close()
    trusted = SkillManager([project_root], project_root=workspace,
                           trust_project=os.path.isfile(
                               os.path.join(workspace, ".sliceagent", "skills-trust")))
    assert trusted.get("repo-skill") is not None

    prior = os.environ.get("AGENT_PROJECT_SKILLS")
    os.environ["AGENT_PROJECT_SKILLS"] = "1"
    try:
        assert SkillManager([project_root], project_root=workspace,
                            trust_project=True).get("repo-skill") is not None
    finally:
        if prior is None:
            os.environ.pop("AGENT_PROJECT_SKILLS", None)
        else:
            os.environ["AGENT_PROJECT_SKILLS"] = prior


if __name__ == "__main__":
    ok = 0
    for fn in CHECKS:
        try:
            fn()
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{ok}/{len(CHECKS)} passed")
    sys.exit(0 if ok == len(CHECKS) else 1)
