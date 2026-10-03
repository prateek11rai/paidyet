"""uv.lock must install for anyone, so it may only point at public PyPI."""

import re

from paidyet.config import ROOT

PUBLIC = {"pypi.org", "files.pythonhosted.org"}


def test_lockfile_points_only_at_public_pypi():
    hosts = set(re.findall(r'https?://([^/"\s]+)', (ROOT / "uv.lock").read_text()))
    assert hosts and hosts <= PUBLIC, (
        f"uv.lock mentions {sorted(hosts - PUBLIC)}: it was probably re-locked against a private "
        "package mirror. Restore it (git checkout uv.lock) and see CLAUDE.md."
    )
