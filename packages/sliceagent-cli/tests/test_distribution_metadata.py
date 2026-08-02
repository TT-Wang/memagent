"""Package-local metadata checks also pin the CI package-test discovery path."""
from pathlib import Path
import tomllib


def test_cli_distribution_declares_its_direct_http_dependency():
    metadata = tomllib.loads(
        (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert "httpx==0.28.1" in metadata["project"]["dependencies"]
