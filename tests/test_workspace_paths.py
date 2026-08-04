"""Tests for workspace/agent path handling in the Delta Chat adapter.

Covers the container->host mapping (with traversal containment) and the
generalized bare-.xdc / MEDIA .xdc extractors. All five cases exercise
pure/near-pure adapter methods and need no live RPC.
"""

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
        assert host.endswith("docker/default/workspace/app.xdc")
        assert "sandboxes" in host

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

    def test_extracts_bare_absolute_non_workspace_xdc(self, platform_config):
        adapter = _make_adapter(platform_config)
        content = "Here is your app at /home/user/proj/app.xdc for you."

        files, _remaining = adapter.extract_local_files(content)

        assert "/home/user/proj/app.xdc" in files

    def test_still_extracts_workspace_xdc(self, platform_config):
        """Regression: Docker /workspace/ paths must still be picked up."""
        adapter = _make_adapter(platform_config)
        content = "Built it: /workspace/app.xdc done."

        files, _remaining = adapter.extract_local_files(content)

        assert "/workspace/app.xdc" in files


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
