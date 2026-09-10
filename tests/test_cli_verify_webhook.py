"""Story 4.3 — ``commishdesk verify-webhook --league <id>``.

Env-var read (unset → one stderr line, exit 1), a malformed / non-Discord URL
(exit 1, no network), a webhook the API rejects (``DeliveryError`` → exit 1, no
traceback, destination not accepted), and the success path (a visible test post
naming the league, exit 0). The demo path resolves the name offline and makes no
Sleeper call; a real id is served by a faked ``SleeperAdapter``. Adding the
subcommand must not regress the bare ``--league … --draft-recap`` callback.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from commishdesk.cli import app
from commishdesk.errors import DeliveryError

runner = CliRunner()

_GOOD_URL = "https://discord.com/api/webhooks/123456789012345678/aB_cD-3fToKeNtOkEn"
_WEBHOOK_VAR = "COMMISHDESK_DISCORD_WEBHOOK_URL"


@pytest.fixture(autouse=True)
def _scrub_webhook_env(monkeypatch) -> None:
    """``COMMISHDESK_DISCORD_WEBHOOK_URL`` must never leak in from the developer's
    shell or a CI image — every test in this module sets it explicitly."""
    monkeypatch.delenv(_WEBHOOK_VAR, raising=False)


def _fake_post(monkeypatch, sink: list | None = None, raises: Exception | None = None):
    def fake(url: str, content: str, *, client: object = None) -> None:
        if sink is not None:
            sink.append((url, content))
        if raises is not None:
            raise raises

    monkeypatch.setattr("commishdesk.deliver.discord.post_discord_text", fake)


def _fake_sleeper(monkeypatch) -> None:
    from commishdesk import demo

    bundle = demo.load_demo_bundle()

    class _FakeAdapter:
        def __init__(self, *a: object, **k: object) -> None: ...

        def fetch(self, league_id: str) -> dict:
            return bundle

        def close(self) -> None: ...

    monkeypatch.setattr("commishdesk.adapters.sleeper.SleeperAdapter", _FakeAdapter)


# --------------------------------------------------------------------------- #
# Env var
# --------------------------------------------------------------------------- #


def test_env_var_unset_is_one_stderr_line_exit_1_no_network(monkeypatch) -> None:
    called = []
    _fake_post(monkeypatch, sink=called)
    result = runner.invoke(app, ["verify-webhook", "--league", "demo"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert result.stderr.strip().count("\n") == 0
    assert _WEBHOOK_VAR in result.stderr
    assert called == []  # no post attempted


# --------------------------------------------------------------------------- #
# Malformed URL
# --------------------------------------------------------------------------- #


def test_malformed_url_is_exit_1_no_network(monkeypatch) -> None:
    # the real post_discord_text rejects a non-Discord URL before any network call
    monkeypatch.setenv(_WEBHOOK_VAR, "https://example.com/hook")
    result = runner.invoke(app, ["verify-webhook", "--league", "demo"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert result.stderr.strip()
    assert result.stderr.strip().count("\n") == 0
    assert "discord.com/api/webhooks" in result.stderr  # names the expected shape


# --------------------------------------------------------------------------- #
# Rejected webhook
# --------------------------------------------------------------------------- #


def test_rejected_webhook_is_reported_and_not_accepted(monkeypatch) -> None:
    monkeypatch.setenv(_WEBHOOK_VAR, _GOOD_URL)
    _fake_post(
        monkeypatch,
        raises=DeliveryError("Discord rejected the webhook (HTTP 401) — deleted or mistyped"),
    )
    result = runner.invoke(app, ["verify-webhook", "--league", "demo"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "HTTP 401" in result.stderr
    assert result.stderr.strip().count("\n") == 0
    assert "accepted" not in result.output  # destination not accepted


# --------------------------------------------------------------------------- #
# Success
# --------------------------------------------------------------------------- #


def test_success_demo_posts_naming_the_league_exit_0_no_sleeper_call(monkeypatch) -> None:
    monkeypatch.setenv(_WEBHOOK_VAR, _GOOD_URL)
    posts: list = []
    _fake_post(monkeypatch, sink=posts)

    # any Sleeper call would explode
    def _boom(*a: object, **k: object):
        raise AssertionError("verify-webhook --league demo must not call Sleeper")

    monkeypatch.setattr("commishdesk.adapters.sleeper.SleeperAdapter", _boom)

    result = runner.invoke(app, ["verify-webhook", "--league", "demo"])
    assert result.exit_code == 0, result.output
    assert len(posts) == 1
    url, content = posts[0]
    assert url == _GOOD_URL
    assert "Trench Warfare" in content  # the demo league name
    assert "Trench Warfare" in result.output
    assert "accepted" in result.output.lower()


def test_success_real_id_resolves_name_via_faked_sleeper(monkeypatch) -> None:
    monkeypatch.setenv(_WEBHOOK_VAR, _GOOD_URL)
    _fake_sleeper(monkeypatch)
    posts: list = []
    _fake_post(monkeypatch, sink=posts)
    result = runner.invoke(app, ["verify-webhook", "--league", "424242"])
    assert result.exit_code == 0, result.output
    assert len(posts) == 1
    assert "Trench Warfare" in posts[0][1]


# --------------------------------------------------------------------------- #
# No regression to the callback path
# --------------------------------------------------------------------------- #


def test_callback_still_resolves_for_a_bare_league_invocation(monkeypatch, tmp_path) -> None:
    result = runner.invoke(
        app, ["--league", "demo", "--draft-recap", "--out-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "The Lead" in result.output
    assert (tmp_path / "commishdesk-demo-draft-recap.html").is_file()
    assert (tmp_path / "commishdesk-demo-draft-recap.email.html").is_file()
    assert (tmp_path / "commishdesk-demo-draft-recap.txt").is_file()


def test_weekly_notice_still_fires_with_the_subcommand_registered() -> None:
    result = runner.invoke(app, ["--league", "123", "--week", "5"])
    assert result.exit_code == 0
    assert "not yet implemented" in result.output


def test_help_lists_the_verify_webhook_command() -> None:
    result = runner.invoke(app, ["--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "verify-webhook" in result.output
