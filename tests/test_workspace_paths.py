"""Tests for configurable workspace path resolution.

Covers issue #3: hardcoded /workspace/ paths break non-Docker deployments.
The adapter must resolve the agent's writable workspace directory dynamically
and map paths under it for delivery, not just literal /workspace/.
"""

import os
from pathlib import Path

import pytest

from adapter import DeltaChatAdapter
from tests.conftest import MockHermesConfig, MockPlatformConfig, MockPlatform


def _make_adapter(extra=None):
    return DeltaChatAdapter(
        MockPlatformConfig(
            name="deltachat-platform",
            platform=MockPlatform.DELTACHAT,
            extra=extra or {"rpc_server": "deltachat-rpc-server"},
            enabled=True,
        )
    )


class TestGetAgentWorkspace:
    """_get_agent_workspace() resolution priority."""

    def test_explicit_config_extra_wins(self, tmp_path):
        """config.extra['workspace'] takes top priority."""
        ws = tmp_path / "my-workspace"
        adapter = _make_adapter(extra={"workspace": str(ws)})
        assert adapter._get_agent_workspace() == str(ws.resolve())

    def test_config_extra_expands_user(self):
        """Tilde in config.extra['workspace'] is expanded."""
        adapter = _make_adapter(extra={"workspace": "~/dc-workspace-test"})
        result = adapter._get_agent_workspace()
        assert not result.startswith("~")
        assert result.endswith("dc-workspace-test")

    def test_fallback_to_hermes_home(self):
        """With no extra config and no terminal.cwd, falls back to hermes home."""
        adapter = _make_adapter()
        expected = str(Path(MockHermesConfig.get_hermes_home()).expanduser().resolve())
        assert adapter._get_agent_workspace() == expected

    def test_empty_workspace_extra_falls_through(self):
        """Blank workspace override is ignored."""
        adapter = _make_adapter(extra={"workspace": "   "})
        expected = str(Path(MockHermesConfig.get_hermes_home()).expanduser().resolve())
        assert adapter._get_agent_workspace() == expected

    def test_terminal_cwd_used_when_no_extra(self, tmp_path, monkeypatch):
        """terminal.cwd from profile config is used when extra has no override."""
        term_cwd = tmp_path / "term-cwd"
        term_cwd.mkdir()

        import gateway.config as gw_config

        monkeypatch.setattr(
            gw_config,
            "get_config",
            lambda: {"terminal": {"cwd": str(term_cwd)}},
            raising=False,
        )
        adapter = _make_adapter()
        assert adapter._get_agent_workspace() == str(term_cwd.resolve())

    def test_terminal_cwd_ignored_when_extra_set(self, tmp_path, monkeypatch):
        """Explicit extra workspace beats terminal.cwd."""
        ws = tmp_path / "explicit-ws"
        term_cwd = tmp_path / "term-cwd"

        import gateway.config as gw_config

        monkeypatch.setattr(
            gw_config,
            "get_config",
            lambda: {"terminal": {"cwd": str(term_cwd)}},
            raising=False,
        )
        adapter = _make_adapter(extra={"workspace": str(ws)})
        assert adapter._get_agent_workspace() == str(ws.resolve())


class TestContainerWorkspaceToHost:
    """_container_workspace_to_host() path mapping."""

    def test_docker_workspace_path_maps_to_sandbox(self):
        """/workspace/<rel> maps to the docker sandbox workspace on host."""
        adapter = _make_adapter()
        result = adapter._container_workspace_to_host("/workspace/app.xdc")
        assert result is not None
        expected_suffix = os.path.join("docker", "default", "workspace", "app.xdc")
        assert result.endswith(expected_suffix)
        assert "sandboxes" in result

    def test_agent_workspace_path_passes_through(self, tmp_path):
        """Paths under the configured agent workspace pass through unchanged."""
        ws = tmp_path / "agent-ws"
        ws.mkdir()
        f = ws / "report.pdf"
        f.write_text("dummy")
        adapter = _make_adapter(extra={"workspace": str(ws)})
        result = adapter._container_workspace_to_host(str(f))
        assert result == str(f)

    def test_unrelated_path_returns_none(self):
        """Paths outside any known workspace root return None."""
        adapter = _make_adapter()
        assert adapter._container_workspace_to_host("/etc/passwd") is None
        assert adapter._container_workspace_to_host("/tmp/stuff.xdc") is None


class TestCopyContainerFileToCache:
    """_copy_container_file_to_cache() copies agent-workspace files to cache."""

    def test_copies_agent_workspace_file(self, tmp_path):
        """A file in the agent workspace is copied to the hermes docs cache."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        os.environ["HERMES_HOME"] = str(hermes_home)

        ws = tmp_path / "agent-ws"
        ws.mkdir()
        src = ws / "app.xdc"
        src.write_bytes(b"PK fake zip")

        adapter = _make_adapter(extra={"workspace": str(ws)})
        cached = adapter._copy_container_file_to_cache(str(src))

        assert cached is not None
        cached_path = Path(cached)
        assert cached_path.is_file()
        assert cached_path.read_bytes() == b"PK fake zip"
        assert cached_path.parent == hermes_home / "cache" / "documents"
        assert cached_path.name == "app.xdc"

    def test_missing_file_returns_none(self, tmp_path):
        """Nonexistent file returns None rather than raising."""
        ws = tmp_path / "agent-ws"
        ws.mkdir()
        adapter = _make_adapter(extra={"workspace": str(ws)})
        assert adapter._copy_container_file_to_cache(str(ws / "nope.xdc")) is None

    def test_unmappable_path_returns_none(self):
        """Path outside workspace roots returns None."""
        adapter = _make_adapter()
        assert adapter._copy_container_file_to_cache("/etc/hostname") is None


class TestFilterDeliveryPaths:
    """filter_*_delivery_paths remap agent-workspace paths through the cache."""

    def test_media_paths_remapped(self, tmp_path):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        os.environ["HERMES_HOME"] = str(hermes_home)

        ws = tmp_path / "agent-ws"
        ws.mkdir()
        src = ws / "app.xdc"
        src.write_bytes(b"PK fake")

        adapter = _make_adapter(extra={"workspace": str(ws)})

        # Base mock doesn't define filter_media_delivery_paths — add a
        # pass-through so the adapter's override can call super().
        from gateway.platforms.base import BasePlatformAdapter

        BasePlatformAdapter.filter_media_delivery_paths = staticmethod(
            lambda files: files
        )
        try:
            result = adapter.filter_media_delivery_paths([(str(src), False)])
        finally:
            del BasePlatformAdapter.filter_media_delivery_paths

        assert len(result) == 1
        out_path, is_voice = result[0]
        assert is_voice is False
        assert str(hermes_home / "cache" / "documents") in out_path
        assert Path(out_path).is_file()

    def test_local_paths_remapped(self, tmp_path):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        os.environ["HERMES_HOME"] = str(hermes_home)

        ws = tmp_path / "agent-ws"
        ws.mkdir()
        src = ws / "notes.pdf"
        src.write_bytes(b"%PDF fake")

        adapter = _make_adapter(extra={"workspace": str(ws)})

        from gateway.platforms.base import BasePlatformAdapter

        BasePlatformAdapter.filter_local_delivery_paths = staticmethod(
            lambda files: files
        )
        try:
            result = adapter.filter_local_delivery_paths([str(src)])
        finally:
            del BasePlatformAdapter.filter_local_delivery_paths

        assert len(result) == 1
        assert str(hermes_home / "cache" / "documents") in result[0]
        assert Path(result[0]).is_file()

    def test_missing_workspace_file_passes_through_with_warning(self, tmp_path, caplog):
        """A path under the workspace that doesn't exist falls through unchanged."""
        ws = tmp_path / "agent-ws"
        ws.mkdir()
        adapter = _make_adapter(extra={"workspace": str(ws)})
        missing = str(ws / "ghost.xdc")

        from gateway.platforms.base import BasePlatformAdapter

        BasePlatformAdapter.filter_media_delivery_paths = staticmethod(
            lambda files: files
        )
        try:
            result = adapter.filter_media_delivery_paths([(missing, False)])
        finally:
            del BasePlatformAdapter.filter_media_delivery_paths

        assert result == [(missing, False)]
