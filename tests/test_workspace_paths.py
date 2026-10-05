"""Tests for workspace/agent path handling in the Delta Chat adapter.

Covers the generalized bare-.xdc / MEDIA .xdc extractors. These exercise
pure/near-pure adapter methods and need no live RPC. Container->host
translation of /workspace/ paths is Hermes's job since 0.21.5.
"""

# conftest.py installs the gateway mocks, so importing adapter here is safe.
from adapter import DeltaChatAdapter


def _make_adapter(platform_config):
    """Construct an adapter without touching RPC (mirrors integration tests)."""
    return DeltaChatAdapter(platform_config)


class TestExtractLocalFiles:
    """Generalized bare-.xdc extractor."""

    def test_extracts_bare_absolute_non_workspace_xdc(
        self, platform_config, tmp_path
    ):
        adapter = _make_adapter(platform_config)
        real_xdc = tmp_path / "app.xdc"
        real_xdc.write_bytes(b"PK\x03\x04")
        content = f"Here is your app at {real_xdc} for you."

        files, remaining = adapter.extract_local_files(content)

        assert str(real_xdc) in files
        assert str(real_xdc) not in remaining

    def test_ignores_bare_xdc_that_does_not_exist(self, platform_config):
        """A path merely mentioned in prose is not an attachment.

        Without the isfile() guard the path is cut out of the reply text and
        pushed at the user as a file that isn't there.
        """
        adapter = _make_adapter(platform_config)
        content = "Save it as /home/user/proj/app.xdc when you are done."

        files, remaining = adapter.extract_local_files(content)

        assert files == []
        assert "/home/user/proj/app.xdc" in remaining

    def test_ignores_xdc_inside_code_block(self, platform_config):
        """The skill's own examples must survive verbatim in the reply."""
        adapter = _make_adapter(platform_config)
        content = (
            "Build it like this:\n"
            "```python\n"
            "zipfile.ZipFile('/workspace/myapp.xdc', 'w')\n"
            "```\n"
        )

        files, remaining = adapter.extract_local_files(content)

        assert files == []
        assert "/workspace/myapp.xdc" in remaining

    def test_still_extracts_workspace_xdc(self, platform_config):
        """Regression: Docker /workspace/ paths must still be picked up.

        These are container-side and never exist on the host, so they are
        exempt from the isfile() guard; Hermes translates them at delivery.
        """
        adapter = _make_adapter(platform_config)
        content = "Built it: /workspace/app.xdc done."

        files, _remaining = adapter.extract_local_files(content)

        assert "/workspace/app.xdc" in files

    def test_removal_leaves_other_occurrences_alone(self, platform_config):
        """Deletion is span-based, not a global str.replace()."""
        adapter = _make_adapter(platform_config)
        content = (
            "Built /workspace/app.xdc.\n"
            "```\n"
            "cp /workspace/app.xdc ./dist/\n"
            "```\n"
        )

        files, remaining = adapter.extract_local_files(content)

        assert files == ["/workspace/app.xdc"]
        # The bare mention is gone; the one inside the code block survives.
        assert remaining.count("/workspace/app.xdc") == 1
        assert "cp /workspace/app.xdc ./dist/" in remaining


class TestExtractMedia:
    """General MEDIA .xdc extractor regression guard."""

    def test_extracts_media_absolute_xdc(self, platform_config):
        adapter = _make_adapter(platform_config)

        media, _remaining = adapter.extract_media("MEDIA:/home/user/app.xdc")

        assert any(p == "/home/user/app.xdc" for p, _ in media)

    def test_extracts_media_home_xdc(self, platform_config):
        adapter = _make_adapter(platform_config)

        media, _remaining = adapter.extract_media("MEDIA:~/app.xdc")

        assert any(p == "~/app.xdc" for p, _ in media)

    def test_extracts_media_workspace_xdc_verbatim(self, platform_config):
        """Docker paths pass through untouched; Hermes translates them later."""
        adapter = _make_adapter(platform_config)

        media, remaining = adapter.extract_media(
            "Here you go. MEDIA:/workspace/myapp.xdc"
        )

        assert media == [("/workspace/myapp.xdc", False)]
        assert "MEDIA:" not in remaining

    def test_ignores_media_xdc_inside_code_block(self, platform_config):
        """A MEDIA: tag shown as documentation is not a delivery request."""
        adapter = _make_adapter(platform_config)
        content = "Emit it like this:\n```\nMEDIA:/workspace/myapp.xdc\n```\n"

        media, remaining = adapter.extract_media(content)

        assert media == []
        assert "MEDIA:/workspace/myapp.xdc" in remaining


class TestDeliveryFiltersNotOverridden:
    """Regression guard for #44.

    Hermes >= 0.21.5 translates /workspace/ container paths in its own
    filter_*_delivery_paths. An adapter override that rewrites those paths
    first (e.g. to a cache copy) hands Hermes a host path it then tries to
    translate as a container path, logging a "did not resolve" warning for
    every delivered file. Cron delivery also bypasses adapter overrides, so
    anything done there would be inconsistent anyway.
    """

    def test_adapter_uses_base_filters(self):
        for name in ("filter_media_delivery_paths", "filter_local_delivery_paths"):
            assert name not in DeltaChatAdapter.__dict__, name

    def test_workspace_paths_reach_base_filter_unchanged(
        self, platform_config, monkeypatch
    ):
        from gateway.platforms.base import BasePlatformAdapter

        seen = {}

        def media(media_files, session_key=""):
            seen["media"] = (list(media_files), session_key)
            return []

        def local(file_paths, session_key=""):
            seen["local"] = (list(file_paths), session_key)
            return []

        monkeypatch.setattr(
            BasePlatformAdapter, "filter_media_delivery_paths", staticmethod(media)
        )
        monkeypatch.setattr(
            BasePlatformAdapter, "filter_local_delivery_paths", staticmethod(local)
        )
        adapter = _make_adapter(platform_config)
        key = "agent:main:deltachat:dm:12"

        adapter.filter_media_delivery_paths(
            [("/workspace/clip.mp4", False)], session_key=key
        )
        adapter.filter_local_delivery_paths(["/workspace/app.xdc"], session_key=key)

        assert seen["media"] == ([("/workspace/clip.mp4", False)], key)
        assert seen["local"] == (["/workspace/app.xdc"], key)
