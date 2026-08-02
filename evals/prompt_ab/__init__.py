"""Scientific A/B test suite for sliceagent's SYSTEM_PROMPT.

Pieces:
  variants.py  — single-variable transforms of the live prompt (the control) → full prompt files.
  metrics.py   — per-trial metric runners (review / convo / tasks), reusing the existing benches.
  stats.py     — paired bootstrap CIs + significance on the per-item deltas.
  run.py       — two-stage screen→confirm driver (paired same-batch control), leaderboard + raw dump.

The variants are injected via the SLICEAGENT_PROMPT_FILE seam in prompt.py (off by default → ships unchanged).
Catalog + citations live in the prompt-ab-catalog memory and the workflow result that generated it.
"""
