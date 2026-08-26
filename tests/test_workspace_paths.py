"""Tests for workspace/agent path handling in the Delta Chat adapter.

Covers the container->host mapping (with traversal containment) and the
generalized bare-.xdc / MEDIA .xdc extractors. All five cases exercise
pure/near-pure adapter methods and need no live RPC.
"""

import os

# conftest.py installs the gateway mocks, so importing adapter here is safe.
from adapter import DeltaChatAdapter


def _make_adapter(platform_config):
    """Construct an adapter without touching RPC (mirrors integration tests)."""
    return DeltaChatAdapter(platform_config)


class TestContainerWorkspaceToHost:
    """_container_workspace_to_host mapping + traversal containment."""

    def test_maps_workspace_path_to_host_sandbox(self, monkeypatch, tmp_path):
        # Make the fallback (get_hermes_home) deterministic. tools.environments
        # is not importable in the test env, so the ImportError branch is used.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        host = DeltaChatAdapter._container_workspace_to_host("/workspace/app.xdc")

        assert host is not None
        # The resolved host path lives under the sandbox workspace root.
        expected = os.path.realpath(
            str(tmp_path / "sandboxes" / "docker" / "default" / "workspace" / "app.xdc")
        )
        assert host == expected

    def test_traversal_escape_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        assert (
            DeltaChatAdapter._container_workspace_to_host(
                "/workspace/../../../etc/passwd"
            )
            is None
        )
        # A .pdf escape is rejected the same way.
        assert (
            DeltaChatAdapter._container_workspace_to_host(
                "/workspace/../../secret/report.pdf"
            )
            is None
        )

    def test_non_workspace_path_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        assert (
            DeltaChatAdapter._container_workspace_to_host("/home/user/app.xdc") is None
        )


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
        exempt from the isfile() guard.
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

    def test_ignores_media_xdc_inside_code_block(self, platform_config):
        """A MEDIA: tag shown as documentation is not a delivery request."""
        adapter = _make_adapter(platform_config)
        content = "Emit it like this:\n```\nMEDIA:/workspace/myapp.xdc\n```\n"

        media, remaining = adapter.extract_media(content)

        assert media == []
        assert "MEDIA:/workspace/myapp.xdc" in remaining
