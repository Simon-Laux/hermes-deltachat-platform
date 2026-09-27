"""Unit tests for pure helper functions in adapter.py.

These tests do not require a running Delta Chat RPC server or Hermes gateway.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from adapter import (
    DC_MESSAGE_MAX_LEN,
    DC_MESSAGE_MAX_LINES,
    DeltaChatAdapter,
    _apply_yaml_config,
    _async_retry,
    _cfg,
    _is_valid_email,
    _parse_csv_unique,
    _parse_email_list,
    _safe_data_dir,
    _split_message,
    _strip_markdown,
    _validate_avatar_path,
    _validate_rpc_server_path,
)


class TestStripMarkdown:
    def test_empty(self):
        assert _strip_markdown("") == ""

    def test_code_block(self):
        assert _strip_markdown("```python\nprint('hi')\n```") == "print('hi')\n"

    def test_inline_code(self):
        assert _strip_markdown("use `cmd` here") == "use cmd here"

    def test_heading(self):
        assert _strip_markdown("# Hello") == "Hello"
        assert _strip_markdown("## World") == "World"

    def test_link(self):
        assert (
            _strip_markdown("[text](https://example.com)")
            == "text (https://example.com)"
        )

    def test_bold_italic_strikethrough(self):
        assert _strip_markdown("**bold**") == "bold"
        assert _strip_markdown("*italic*") == "italic"
        assert _strip_markdown("__bold__") == "bold"
        assert _strip_markdown("_italic_") == "italic"
        assert _strip_markdown("~~strike~~") == "strike"

    def test_closed_atx_heading(self):
        assert _strip_markdown("## Section ##") == "Section"

    def test_bullet_markers_normalized(self):
        out = _strip_markdown("* first\n+ second\n- third\n  * nested")
        assert out == "- first\n- second\n- third\n  - nested"

    def test_numbered_list_kept_as_plain_text(self):
        assert _strip_markdown("1. one\n2. two") == "1. one\n2. two"

    def test_fenced_code_keeps_body(self):
        out = _strip_markdown("```js\nconst x = 1;\n```")
        assert "```" not in out
        assert "const x = 1;" in out

    def test_urls_and_punctuation_untouched(self):
        text = "See https://example.com/a?b=1&c=2 — it's fine, e.g. this."
        assert _strip_markdown(text) == text

    def test_unicode_preserved(self):
        text = "Ahoj, jak se máš? 日本語 — café ☕"
        assert _strip_markdown(text) == text

    def test_pipe_table_flattened_to_label_value_lines(self):
        out = _strip_markdown(
            "| Name | Role |\n" "| --- | --- |\n" "| Alice | Dev |\n" "| Bob | PM |"
        )
        assert "|" not in out
        assert out == "Name: Alice, Role: Dev\nName: Bob, Role: PM"

    def test_table_without_outer_pipes(self):
        out = _strip_markdown("a | b\n--- | ---\n1 | 2")
        assert out == "a: 1, b: 2"

    def test_table_with_alignment_colons(self):
        out = _strip_markdown("| L | R |\n|:---|---:|\n| x | y |")
        assert out == "L: x, R: y"

    def test_table_surrounded_by_prose(self):
        out = _strip_markdown("Here:\n\n| K | V |\n| - | - |\n| a | b |\n\nDone.")
        assert out == "Here:\n\nK: a, V: b\n\nDone."

    def test_non_table_pipes_left_alone(self):
        text = "run `foo | bar` then check\nresult is a | b here"
        assert _strip_markdown(text) == "run foo | bar then check\nresult is a | b here"


class TestSplitMessage:
    def test_short_unchanged(self):
        assert _split_message("hello") == ["hello"]

    def test_empty(self):
        assert _split_message("") == []

    def test_exact_boundary_no_split(self):
        text = "a" * DC_MESSAGE_MAX_LEN
        assert _split_message(text) == [text]

    def test_split_on_paragraph(self):
        text = ("a" * 1800) + "\n\n" + ("b" * 1800)
        parts = _split_message(text)
        assert len(parts) == 2
        assert all(len(p) <= DC_MESSAGE_MAX_LEN for p in parts)

    def test_split_on_line(self):
        text = ("a" * 1800) + "\n" + ("b" * 1800)
        parts = _split_message(text)
        assert len(parts) == 2

    def test_hard_split_fallback(self):
        text = "x" * 8000
        parts = _split_message(text)
        assert len(parts) >= 2
        assert all(len(p) <= DC_MESSAGE_MAX_LEN for p in parts)
        assert "".join(parts) == text

    def test_respects_custom_max_len(self):
        text = "a" * 100
        parts = _split_message(text, max_len=40)
        assert all(len(p) <= 40 for p in parts)
        assert "".join(parts) == text

    def test_zero_or_negative_max_len_uses_default(self):
        text = "a" * (DC_MESSAGE_MAX_LEN + 100)
        for bad_max in (0, -1, -1000):
            parts = _split_message(text, max_len=bad_max)
            assert all(len(p) <= DC_MESSAGE_MAX_LEN for p in parts)
            assert "".join(parts) == text

    def test_splits_on_line_count(self):
        text = "\n".join(f"line {i}" for i in range(23))
        parts = _split_message(text, max_lines=5)
        assert len(parts) == 5  # 5+5+5+5+3
        assert all(p.count("\n") + 1 <= 5 for p in parts)
        # ordering preserved, nothing dropped
        assert "\n".join(parts).split("\n") == text.split("\n")

    def test_exact_line_boundary_not_split(self):
        text = "\n".join(f"line {i}" for i in range(5))
        assert _split_message(text, max_lines=5) == [text]

    def test_one_over_line_boundary_splits(self):
        text = "\n".join(f"line {i}" for i in range(6))
        parts = _split_message(text, max_lines=5)
        assert len(parts) == 2
        assert parts[0] == "\n".join(f"line {i}" for i in range(5))
        assert parts[1] == "line 5"

    def test_long_paragraph_wraps_within_char_limit(self):
        text = " ".join(["word"] * 400)  # ~2000 chars, single line
        parts = _split_message(text, max_len=120, max_lines=20)
        assert len(parts) > 1
        assert all(len(p) <= 120 for p in parts)
        assert " ".join(parts).split() == text.split()

    def test_list_items_kept_on_their_own_lines(self):
        text = "\n".join(f"- item {i}" for i in range(12))
        parts = _split_message(text, max_lines=4)
        assert len(parts) == 3
        for p in parts:
            for line in p.split("\n"):
                assert line.startswith("- item ")

    def test_unicode_not_split_mid_codepoint(self):
        # base 'e' + combining acute accent (U+0301); a naive character
        # split at the limit would land between the base char and its mark.
        text = "e\u0301" * 200
        parts = _split_message(text, max_len=101, max_lines=20)
        assert "".join(parts) == text
        for p in parts:
            assert not p.endswith("e")  # never a base char without its mark

    def test_default_line_limit_applies(self):
        text = "\n".join(f"row {i}" for i in range(DC_MESSAGE_MAX_LINES + 5))
        parts = _split_message(text)
        assert len(parts) == 2


class TestWorkspacePathMapping:
    def test_rejects_dotdot(self):
        assert (
            DeltaChatAdapter._container_workspace_to_host("/workspace/../etc/passwd")
            is None
        )
        assert (
            DeltaChatAdapter._container_workspace_to_host(
                "/workspace/subdir/../../etc/passwd"
            )
            is None
        )

    def test_rejects_symlink_escape(self, tmp_path, monkeypatch):
        from gateway.config import get_hermes_home

        hermes_home = tmp_path / "home"
        hermes_home.mkdir()
        workspace = hermes_home / "sandboxes" / "docker" / "default" / "workspace"
        workspace.mkdir(parents=True)
        secret = tmp_path / "secret.txt"
        secret.write_text("secret")
        (workspace / "link").symlink_to(secret)

        monkeypatch.setattr("gateway.config.get_hermes_home", lambda: str(hermes_home))
        assert DeltaChatAdapter._container_workspace_to_host("/workspace/link") is None

    def test_maps_normal_workspace_path(self, tmp_path, monkeypatch):
        from gateway.config import get_hermes_home

        hermes_home = tmp_path / "home"
        workspace = hermes_home / "sandboxes" / "docker" / "default" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "file.txt").write_text("ok")

        monkeypatch.setattr("gateway.config.get_hermes_home", lambda: str(hermes_home))
        result = DeltaChatAdapter._container_workspace_to_host("/workspace/file.txt")
        assert result == str(workspace / "file.txt")


class TestEmailValidation:
    def test_valid(self):
        for e in ["user@example.com", "a+b@x.org", "foo@bar.co.uk"]:
            assert _is_valid_email(e), e

    def test_invalid(self):
        for e in ["notanemail", "@domain.com", "user@", "user @domain.com"]:
            assert not _is_valid_email(e), e

    def test_rejects_display_name_form(self):
        assert not _is_valid_email("User <user@example.com>")

    def test_rejects_too_long(self):
        assert not _is_valid_email("a" * 250 + "@x.com")


class TestParseEmailList:
    def test_basic(self):
        assert _parse_email_list("a@x.com, B@X.COM") == {"a@x.com", "b@x.com"}

    def test_empty(self):
        assert _parse_email_list("") == set()

    def test_single(self):
        assert _parse_email_list("alice@example.com") == {"alice@example.com"}


class TestParseChatmailServers:
    def test_basic(self):
        assert _parse_csv_unique("a.com, b.com, a.com") == ["a.com", "b.com"]

    def test_empty(self):
        assert _parse_csv_unique("") == []

    def test_whitespace_trimmed(self):
        assert _parse_csv_unique(" a.com , b.com ") == ["a.com", "b.com"]

    def test_case_preserved(self):
        assert _parse_csv_unique("A.com, a.com") == ["A.com"]


class TestSafeDataDir:
    def test_rejects_dotdot(self):
        with pytest.raises(ValueError):
            _safe_data_dir("/tmp/foo/../bar")

    def test_creates_directory(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "dc-data")
            p = _safe_data_dir(path, create=True)
            assert p.exists()
            assert p.stat().st_mode & 0o777 == 0o700


class TestValidateRpcServerPath:
    def test_non_strict_returns_path_when_missing(self):
        assert (
            _validate_rpc_server_path("probably-not-on-path", strict=False)
            == "probably-not-on-path"
        )

    def test_strict_missing_raises(self):
        with pytest.raises(ValueError):
            _validate_rpc_server_path("/nonexistent/binary-12345", strict=True)

    def test_strict_resolves_absolute_executable(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"#!/bin/sh\n")
            path = f.name
        os.chmod(path, 0o755)
        try:
            assert _validate_rpc_server_path(path, strict=True) == path
        finally:
            os.unlink(path)


class TestValidateAvatarPath:
    def test_non_strict_accepts_image_suffix(self):
        assert _validate_avatar_path("/tmp/bot.png", strict=False) == "/tmp/bot.png"

    def test_invalid_suffix_raises(self):
        with pytest.raises(ValueError):
            _validate_avatar_path("/tmp/bot.txt", strict=False)

    def test_strict_missing_file_raises(self):
        with pytest.raises(ValueError):
            _validate_avatar_path("/tmp/nonexistent.png", strict=True)


class TestAsyncRetry:
    @pytest.mark.asyncio
    async def test_succeeds_first_try(self):
        coro = AsyncMock(return_value="ok")
        result = await _async_retry(coro, max_attempts=3, base_delay=0.01)
        assert result == "ok"
        assert coro.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self):
        coro = AsyncMock(side_effect=[RuntimeError("boom"), "ok"])
        result = await _async_retry(coro, max_attempts=3, base_delay=0.01)
        assert result == "ok"
        assert coro.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_exhaustion(self):
        coro = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            await _async_retry(coro, max_attempts=2, base_delay=0.01)
        assert coro.call_count == 2


class TestCfg:
    class _FakeConfig:
        def __init__(self, extra):
            self.extra = extra

    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv("DELTACHAT_TEST_KEY", "env_value")
        config = self._FakeConfig({"test_key": "extra_value"})
        assert _cfg(config, "DELTACHAT_TEST_KEY", "test_key", "default") == "env_value"

    def test_extra_fallback(self, monkeypatch):
        monkeypatch.delenv("DELTACHAT_TEST_KEY", raising=False)
        config = self._FakeConfig({"test_key": "extra_value"})
        assert (
            _cfg(config, "DELTACHAT_TEST_KEY", "test_key", "default") == "extra_value"
        )

    def test_default_fallback(self, monkeypatch):
        monkeypatch.delenv("DELTACHAT_TEST_KEY", raising=False)
        config = self._FakeConfig({})
        assert _cfg(config, "DELTACHAT_TEST_KEY", "test_key", "default") == "default"


class TestMentionDetection:
    """Unit tests for the group mention detector."""

    def test_at_mention_matches(self, platform_config):
        platform_config.extra = {"display_name": "Hermes"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("Hello @Hermes, how are you?") is True

    def test_bare_name_without_at_does_not_match(self, platform_config):
        # Bare name in prose can be about the bot without addressing it
        # (e.g. "napis Alici" = ask someone else to message Alice), so a
        # mention requires an explicit "@" prefix.
        platform_config.extra = {"display_name": "Hermes"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("Hermes please help") is False

    def test_case_insensitive(self, platform_config):
        platform_config.extra = {"display_name": "Hermes"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("hey @hermes") is True
        assert adapter._is_mentioned("@HERMES do this") is True

    def test_substring_does_not_match(self, platform_config):
        platform_config.extra = {"display_name": "Hermes"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("@Hermesssss") is False
        assert adapter._is_mentioned("@Hermesss") is False

    def test_punctuation_boundary_still_matches(self, platform_config):
        platform_config.extra = {"display_name": "Hermes"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("@Hermes!") is True
        assert adapter._is_mentioned("(@Hermes)") is True

    def test_empty_text_is_not_mentioned(self, platform_config):
        platform_config.extra = {"display_name": "Hermes"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("") is False


class TestCzechDeclensionMentions:
    """Czech names decline by case — mentions must survive that."""

    def test_alice_dative_and_instrumental(self, platform_config):
        platform_config.extra = {"display_name": "Alice"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("napiš @Alici, aby to udělala") is True
        assert adapter._is_mentioned("mluvil jsem s @Alicí") is True
        assert adapter._is_mentioned("@Alice, jsi tu?") is True
        # bare name, no "@" — about Alice, not addressed to her
        assert adapter._is_mentioned("napiš Alici, aby to udělala") is False

    def test_anikke_vocative_and_indeclinable_form(self, platform_config):
        platform_config.extra = {"display_name": "Anikke"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("zeptej se @Anikke") is True
        # Bots sometimes wrongly decline this indeclinable name; tolerated
        # on the receiving end too, even though the LLM shouldn't produce it.
        assert adapter._is_mentioned("@Anikko, co si o tom myslíš?") is True

    def test_holly_dative_unchanged(self, platform_config):
        platform_config.extra = {"display_name": "Holly"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("řekni to @Holly") is True

    def test_short_name_does_not_stem_match(self, platform_config):
        # "Tom" stems to "To" (2 chars, below the 3-char safety floor), so
        # this must fall back to an exact match rather than risk matching
        # unrelated words like "Tomas" or "Tone".
        platform_config.extra = {"display_name": "Tom"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("@Tom, jsi tu?") is True
        assert adapter._is_mentioned("@Tomasi, jsi tu?") is False

    def test_mention_aliases_config(self, platform_config):
        platform_config.extra = {
            "display_name": "Anikke",
            "mention_aliases": "Anička, Aničko",
        }
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("@Aničko, pomoz mi") is True
        assert adapter._is_mentioned("@Anička už odpověděla") is True

    def test_unrelated_word_sharing_prefix_does_not_match(self, platform_config):
        # "Hollywood" shares the "Holl" stem but has far more than 2 extra
        # trailing characters, so it must not count as a mention of Holly.
        platform_config.extra = {"display_name": "Holly"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._is_mentioned("I watched a @Hollywood movie") is False


class TestApplyYamlConfig:
    def test_carries_forward_existing_extra(self):
        platform_cfg = {"extra": {"foo": "bar"}}
        seeded = _apply_yaml_config({}, platform_cfg)
        assert seeded["foo"] == "bar"

    def test_access_control_keys_are_bridged(self):
        platform_cfg = {
            "extra": {},
            "allowed_users": "a@example.com,b@example.com",
            "allow_all_users": True,
            "dm_allowed_users": "a@example.com",
            "group_allowed_users": "b@example.com",
            "dm_policy": "allowlist",
            "group_policy": "allowlist",
        }
        seeded = _apply_yaml_config({}, platform_cfg)
        assert seeded["allowed_users"] == "a@example.com,b@example.com"
        assert seeded["allow_all_users"] is True
        assert seeded["dm_allowed_users"] == "a@example.com"
        assert seeded["group_allowed_users"] == "b@example.com"
        assert seeded["dm_policy"] == "allowlist"
        assert seeded["group_policy"] == "allowlist"

    def test_yaml_keys_override_carried_forward_extra(self):
        platform_cfg = {"extra": {"dm_policy": "open"}, "dm_policy": "allowlist"}
        seeded = _apply_yaml_config({}, platform_cfg)
        assert seeded["dm_policy"] == "allowlist"


class TestBotExchangeGuard:
    def test_disabled_without_human_users(self, platform_config):
        platform_config.extra = {"max_bot_exchanges": 2}
        adapter = DeltaChatAdapter(platform_config)
        for _ in range(10):
            should_process, _ = adapter._check_bot_exchange_guard("chat1", "bot-a@x")
            assert should_process is True

    def test_trips_across_alternating_senders(self, platform_config):
        platform_config.extra = {
            "human_users": "tom@x",
            "max_bot_exchanges": 2,
        }
        adapter = DeltaChatAdapter(platform_config)
        senders = ["bot-a@x", "bot-b@x", "bot-c@x", "bot-a@x"]
        results = [adapter._check_bot_exchange_guard("chat1", s)[0] for s in senders]
        # 3rd message (count=3) exceeds max_bot_exchanges=2, regardless of
        # each message coming from a different sender.
        assert results == [True, True, False, False]

    def test_human_message_resets_the_count(self, platform_config):
        platform_config.extra = {
            "human_users": "tom@x",
            "max_bot_exchanges": 2,
        }
        adapter = DeltaChatAdapter(platform_config)
        adapter._check_bot_exchange_guard("chat1", "bot-a@x")
        adapter._check_bot_exchange_guard("chat1", "bot-b@x")
        adapter._check_bot_exchange_guard("chat1", "tom@x")
        should_process, _ = adapter._check_bot_exchange_guard("chat1", "bot-c@x")
        assert should_process is True

    def test_should_warn_only_once_per_trip(self, platform_config):
        platform_config.extra = {
            "human_users": "tom@x",
            "max_bot_exchanges": 1,
        }
        adapter = DeltaChatAdapter(platform_config)
        adapter._check_bot_exchange_guard("chat1", "bot-a@x")
        _, warn1 = adapter._check_bot_exchange_guard("chat1", "bot-b@x")
        _, warn2 = adapter._check_bot_exchange_guard("chat1", "bot-c@x")
        assert warn1 is True
        assert warn2 is False


class TestFreeResponseChannels:
    @pytest.mark.asyncio
    async def test_require_mention_blocks_unlisted_group(self, platform_config):
        platform_config.extra = {"require_mention": True, "display_name": "Bot"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.send = AsyncMock()
        assert await adapter._check_mention("hello", "group", "13") is False
        adapter.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_free_response_channel_skips_mention_requirement(
        self, platform_config
    ):
        platform_config.extra = {
            "require_mention": True,
            "display_name": "Bot",
            "free_response_channels": "13",
        }
        adapter = DeltaChatAdapter(platform_config)
        adapter.send = AsyncMock()
        assert await adapter._check_mention("hello", "group", "13") is True

    @pytest.mark.asyncio
    async def test_free_response_channel_does_not_affect_other_chats(
        self, platform_config
    ):
        platform_config.extra = {
            "require_mention": True,
            "display_name": "Bot",
            "free_response_channels": "13",
        }
        adapter = DeltaChatAdapter(platform_config)
        adapter.send = AsyncMock()
        assert await adapter._check_mention("hello", "group", "14") is False

    @pytest.mark.asyncio
    async def test_multiple_free_response_channels(self, platform_config):
        platform_config.extra = {
            "require_mention": True,
            "display_name": "Bot",
            "free_response_channels": "13, 14",
        }
        adapter = DeltaChatAdapter(platform_config)
        adapter.send = AsyncMock()
        assert await adapter._check_mention("hello", "group", "13") is True
        assert await adapter._check_mention("hello", "group", "14") is True
        assert await adapter._check_mention("hello", "group", "15") is False


class TestUnmentionedGroupMessageIsSilent:
    """The mention gate must never reply — see CLAUDE.md bot-loop history:
    every bot in a shared group enforcing this independently would spam a
    'please mention me' notice per bot per unmentioned message."""

    @pytest.mark.asyncio
    async def test_no_reply_sent_when_unmentioned(self, platform_config):
        platform_config.extra = {
            "require_mention": True,
            "display_name": "Bot",
            "send_rejection_replies": True,
        }
        adapter = DeltaChatAdapter(platform_config)
        adapter.send = AsyncMock()

        result = await adapter._check_mention("just chatting", "group", "13")

        assert result is False
        adapter.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_mentioned_message_still_processes(self, platform_config):
        platform_config.extra = {"require_mention": True, "display_name": "Bot"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.send = AsyncMock()

        result = await adapter._check_mention("hey @Bot, you there?", "group", "13")

        assert result is True
        adapter.send.assert_not_called()


class TestSharedHelpers:
    """Helpers extracted from duplicated call sites."""

    def test_is_destructive(self):
        from adapter import _is_destructive

        assert _is_destructive("delete_chat")
        assert _is_destructive("remove_contact_from_chat")
        assert _is_destructive("leave_group")
        assert not _is_destructive("get_chat_contacts")

    def test_bounded_int(self):
        from adapter import _bounded_int

        assert _bounded_int("150", 100, 10000) == 150
        assert _bounded_int(100, 100, 10000) == 100
        assert _bounded_int("99", 100, 10000) is None
        assert _bounded_int("abc", 100, 10000) is None
        assert _bounded_int(None, 1, 2) is None

    def test_invalid_message_limits_fall_back_to_defaults(
        self, platform_config, monkeypatch
    ):
        monkeypatch.delenv("DELTACHAT_MAX_MESSAGE_LENGTH", raising=False)
        monkeypatch.delenv("DELTACHAT_MAX_MESSAGE_LINES", raising=False)
        platform_config.extra = {"max_message_length": "5", "max_message_lines": "x"}
        adapter = DeltaChatAdapter(platform_config)
        assert adapter._max_message_len == DC_MESSAGE_MAX_LEN
        assert adapter._max_message_lines == DC_MESSAGE_MAX_LINES

    def test_validate_config_rejects_out_of_bounds_limit(
        self, platform_config, monkeypatch
    ):
        import adapter as adapter_mod

        monkeypatch.delenv("DELTACHAT_MAX_MESSAGE_LINES", raising=False)
        monkeypatch.setattr(adapter_mod, "check_requirements", lambda: True)
        platform_config.extra = {"max_message_lines": "500"}
        with pytest.raises(ValueError, match="DELTACHAT_MAX_MESSAGE_LINES"):
            adapter_mod.validate_config(platform_config)

    @pytest.mark.asyncio
    async def test_gate_inbound_accepts_contact_request(
        self, platform_config, mock_rpc
    ):
        platform_config.extra = {"dm_policy": "open"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc, adapter.account_id = mock_rpc, 1
        mock_rpc.get_contact = AsyncMock(return_value={"address": "u@example.com"})
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Single", "is_contact_request": True}
        )
        mock_rpc.accept_chat = AsyncMock()

        assert await adapter._gate_inbound(5, 1, 7) is True
        mock_rpc.accept_chat.assert_awaited_once_with(1, 5)

    @pytest.mark.asyncio
    async def test_gate_inbound_rejected_group_invite_is_left_silently(
        self, platform_config, mock_rpc
    ):
        platform_config.extra = {"group_policy": "disabled"}
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc, adapter.account_id = mock_rpc, 1
        adapter.send = AsyncMock()
        mock_rpc.get_contact = AsyncMock(return_value={"address": "u@example.com"})
        mock_rpc.get_basic_chat_info = AsyncMock(
            return_value={"chat_type": "Group", "is_contact_request": True}
        )
        mock_rpc.leave_group = AsyncMock()
        mock_rpc.accept_chat = AsyncMock()

        assert await adapter._gate_inbound(5, 2, 7) is False
        mock_rpc.leave_group.assert_awaited_once_with(1, 5)
        mock_rpc.accept_chat.assert_not_called()
        adapter.send.assert_not_called()
        assert adapter._stats == {"messages_rejected": 1}

    @pytest.mark.asyncio
    async def test_send_voice_uses_voice_viewtype_and_counts(
        self, platform_config, mock_rpc, tmp_path
    ):
        audio = tmp_path / "v.ogg"
        audio.write_bytes(b"ogg")
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc, adapter.account_id = mock_rpc, 1
        mock_rpc.send_msg = AsyncMock(return_value=9)

        result = await adapter.send_voice("3", str(audio), caption="hi")

        assert result.success is True and result.message_id == "9"
        msg = mock_rpc.send_msg.await_args.args[2]
        assert msg.viewtype.name == "VOICE" and msg.file == str(audio)
        assert adapter._stats == {"voices_sent": 1}

    @pytest.mark.asyncio
    async def test_send_file_failure_counts(self, platform_config, mock_rpc):
        adapter = DeltaChatAdapter(platform_config)
        adapter.rpc, adapter.account_id = mock_rpc, 1
        mock_rpc.send_msg = AsyncMock(side_effect=RuntimeError("boom"))

        result = await adapter.send_file("3", "/x.pdf", reply_to="4")

        assert result.success is False and "boom" in result.error
        assert adapter._stats == {"files_send_failed": 1}
