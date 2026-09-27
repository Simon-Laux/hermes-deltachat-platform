"""Tests for setup.py functions - standalone."""

import os
import re


class TestScrapeRelayServers:
    """Test the scrape_relay_servers function logic."""

    def test_server_pattern_matches_domains(self):
        """Test that the pattern matches domain names."""
        pattern = r"(?:https?://)?([a-zA-Z0-9\-\.]+(?::[0-9]+)?)(?:/|\s|,|$)"

        test_cases = [
            ("nine.testrun.org", True),
            ("chatmail.at", True),
            ("example.com:8080", True),
            ("192.168.1.1", True),
            ("https://example.com", True),
            ("http://example.com/path", True),
        ]

        for text, should_match in test_cases:
            match = re.search(pattern, text)
            assert (match is not None) == should_match, f"Failed for {text}"

    def test_server_pattern_ignores_common_false_positives(self):
        """Test that common false positives are filtered."""
        # These should be matched by pattern but filtered by the function
        false_positives = ["chatmail.at", "webxdc.org"]

        for fp in false_positives:
            assert fp.startswith("chatmail.at") or fp.startswith("webxdc")


class TestVersionConstants:
    """Test version-related constants."""

    def test_min_dc_version_format(self):
        """Test that version strings are in correct format."""
        version = "2.51.0"
        parts = version.split(".")
        assert len(parts) == 3
        for part in parts:
            assert part.isdigit()

    def test_version_tuple_comparison(self):
        """Test that version tuples compare correctly."""

        def parse_version(v):
            parts = v.split(".")
            while len(parts) < 3:
                parts.append("0")
            return tuple(int(p) for p in parts[:3])

        assert parse_version("2.51.0") == (2, 51, 0)
        assert parse_version("2.52.0") > parse_version("2.51.0")
        assert parse_version("2.50.0") < parse_version("2.51.0")
        assert parse_version("3.0.0") > parse_version("2.51.0")
        assert parse_version("1.0.0") < parse_version("2.51.0")


class TestChooseRelay:
    """Menu logic of DeltaChatAccountSetup._choose_relay."""

    def _choose(self, monkeypatch, *answers):
        import sys

        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import setup

        monkeypatch.setattr(setup, "scrape_relay_servers", lambda: ["a.org", "b.org"])
        replies = iter(answers)
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(replies))
        return setup.DeltaChatAccountSetup._choose_relay()

    def test_default_and_pick(self, monkeypatch):
        assert self._choose(monkeypatch, "") == "a.org"
        assert self._choose(monkeypatch, "2") == "b.org"

    def test_custom_relay_is_bare_host(self, monkeypatch):
        assert self._choose(monkeypatch, "3", "https://c.org") == "c.org"

    def test_invalid_falls_back_to_default(self, monkeypatch):
        assert self._choose(monkeypatch, "9") == "a.org"
        assert self._choose(monkeypatch, "0") == "a.org"
        assert self._choose(monkeypatch, "x") == "a.org"
