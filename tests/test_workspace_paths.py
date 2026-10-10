"""Tests for workspace/agent path handling in the Delta Chat adapter.

Covers the generalized bare-.xdc / MEDIA .xdc extractors. These exercise
pure/near-pure adapter methods and need no live RPC. Container->host
translation of /workspace/ paths is Hermes's job since 0.21.5.
"""

import pytest

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

    def test_bare_workspace_xdc_stays_text(self, platform_config):
        """A bare /workspace/ path is not delivered; the agent uses MEDIA:."""
        adapter = _make_adapter(platform_config)
        content = "Built it: /workspace/app.xdc done."

        files, remaining = adapter.extract_local_files(content)

        assert files == []
        assert remaining == content

    def test_home_and_absolute_spelling_are_one_file(
        self, platform_config, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "app.xdc").write_bytes(b"PK\x03\x04")
        adapter = _make_adapter(platform_config)

        files, remaining = adapter.extract_local_files(
            f"See ~/app.xdc or {tmp_path}/app.xdc."
        )

        assert files == [str(tmp_path / "app.xdc")]
        assert "app.xdc" not in remaining

    def test_removed_path_leaves_no_blank_line_run(self, platform_config, tmp_path):
        adapter = _make_adapter(platform_config)
        app = tmp_path / "app.xdc"
        app.write_bytes(b"PK\x03\x04")

        _files, remaining = adapter.extract_local_files(f"a\n\n{app}\n\nb")

        assert remaining == "a\n\nb"

    def test_removal_leaves_other_occurrences_alone(self, platform_config, tmp_path):
        """Deletion is span-based, not a global str.replace()."""
        adapter = _make_adapter(platform_config)
        app = tmp_path / "app.xdc"
        app.write_bytes(b"PK\x03\x04")
        content = (
            f"Built {app}.\n"
            "```\n"
            f"cp {app} ./dist/\n"
            "```\n"
        )

        files, remaining = adapter.extract_local_files(content)

        assert files == [str(app)]
        # The bare mention is gone; the one inside the code block survives.
        assert remaining.count(str(app)) == 1
        assert f"cp {app} ./dist/" in remaining


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

    def test_media_xdc_stops_at_first_xdc(self, platform_config):
        """A spaced path must not run on to a later .xdc and eat the prose."""
        adapter = _make_adapter(platform_config)

        media, remaining = adapter.extract_media(
            "MEDIA:/workspace/a b.xdc and /workspace/c.xdc too"
        )

        assert media == [("/workspace/a b.xdc", False)]
        assert remaining == "and /workspace/c.xdc too"

    def test_media_xdc_before_sentence_dot(self, platform_config):
        adapter = _make_adapter(platform_config)

        media, _remaining = adapter.extract_media("Done: MEDIA:/workspace/app.xdc.")

        assert media == [("/workspace/app.xdc", False)]

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
        adapter.filter_local_delivery_paths(["/workspace/report.pdf"], session_key=key)

        assert seen["media"] == ([("/workspace/clip.mp4", False)], key)
        assert seen["local"] == (["/workspace/report.pdf"], key)


class TestNoDoubleDelivery:
    """A file named more than once in one reply goes out once."""

    async def _deliver(self, adapter, media, local):
        await adapter._deliver_media_attachments(
            object(), media, local, force_document_attachments=False,
            human_delay=0, metadata={}, record_delivery=lambda r: None)
        return adapter.delivered

    @pytest.mark.asyncio
    async def test_bare_path_of_tagged_file_is_dropped(self, platform_config, tmp_path):
        adapter = _make_adapter(platform_config)
        app = tmp_path / "app.xdc"
        app.write_bytes(b"PK\x03\x04")
        media, rest = adapter.extract_media(f"Here it is:\nMEDIA:{app}\nSaved at {app}.")
        local, _ = adapter.extract_local_files(rest)
        assert local == [str(app)]  # both extractors see it ...

        media, local, kwargs = await self._deliver(adapter, media, local)

        assert media == [(str(app), False)]
        assert local == []  # ... but it is sent once
        assert kwargs["human_delay"] == 0  # the rest is passed through

    @pytest.mark.asyncio
    async def test_paths_compare_resolved(self, platform_config, tmp_path, monkeypatch):
        """~ and symlinked spellings of the same file count as the same file."""
        monkeypatch.setenv("HOME", str(tmp_path))
        real = tmp_path / "real.pdf"
        real.write_bytes(b"%PDF")
        (tmp_path / "link.pdf").symlink_to(real)
        adapter = _make_adapter(platform_config)

        _, local, _ = await self._deliver(
            adapter, [("~/real.pdf", False)], [str(tmp_path / "link.pdf")])

        assert local == []

    @pytest.mark.asyncio
    async def test_other_bare_paths_are_kept(self, platform_config, tmp_path):
        adapter = _make_adapter(platform_config)
        a, b, c = (str(tmp_path / n) for n in ("a.pdf", "b.pdf", "c.pdf"))

        media, local, _ = await self._deliver(adapter, [(a, False)], [b, a, c])

        assert media == [(a, False)]
        assert local == [b, c]  # only the tagged file is dropped, order kept

    @pytest.mark.asyncio
    async def test_repeats_within_a_list_are_dropped(self, platform_config, tmp_path):
        """Two spellings that Hermes resolves to one path, in either list."""
        adapter = _make_adapter(platform_config)
        a, b = str(tmp_path / "a.png"), str(tmp_path / "b.pdf")

        media, local, _ = await self._deliver(
            adapter, [(a, False), (a, False)], [b, b])

        assert media == [(a, False)]
        assert local == [b]

    @pytest.mark.asyncio
    async def test_empty_lists(self, platform_config):
        adapter = _make_adapter(platform_config)

        assert (await self._deliver(adapter, [], []))[:2] == ([], [])
