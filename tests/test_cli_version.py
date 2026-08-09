"""
Regression test: `bitscrape --version` previously printed a hardcoded
"0.1.0" string regardless of what was actually installed. Now it reads the
real installed distribution version via importlib.metadata.
"""

from __future__ import annotations

from unittest.mock import patch

from bitscrape.cli.main import _installed_version
from importlib.metadata import PackageNotFoundError


def test_installed_version_matches_real_metadata():
    from importlib.metadata import version

    assert _installed_version() == version("bitscrape")


def test_falls_back_gracefully_if_not_installed():
    with patch("bitscrape.cli.main._pkg_version", side_effect=PackageNotFoundError):
        assert _installed_version() == "0.0.0+unknown"
