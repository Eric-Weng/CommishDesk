"""Story 7.4 — ``mail_identities`` normalisation beyond the I7 invariant test."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from commishdesk.deliver import identity
from commishdesk.deliver.identity import MailIdentities, mail_identities
from commishdesk.errors import CommishDeskError, DeliveryError, MailIdentityError


def test_distinct_identities_are_normalised() -> None:
    got = mail_identities(" Confirm <Hi@News.X.test.> ", "mail.x.test:587")
    assert got == MailIdentities("news.x.test", "mail.x.test")


@pytest.mark.parametrize(
    "bulk",
    [
        "MAIL.x.TEST",
        "Digest <a@mail.x.test>",
        "mail.x.test.",
        "a@mail.x.test:25",
        "b@mail.x.test",
    ],
)
def test_same_domain_in_any_spelling_is_refused(bulk: str) -> None:
    with pytest.raises(MailIdentityError):
        mail_identities("a@Mail.x.test", bulk)


def test_parent_and_child_domains_are_distinct() -> None:
    assert mail_identities("x.test", "mail.x.test").bulk == "mail.x.test"


@pytest.mark.parametrize("blank", ["", " ", "@", "n <>", "a b"])
def test_blank_or_unusable_is_refused(blank: str) -> None:
    with pytest.raises(MailIdentityError):
        mail_identities(blank, "mail.x.test")


def test_error_is_a_commishdesk_error_but_not_a_delivery_error() -> None:
    assert issubclass(MailIdentityError, CommishDeskError)
    assert not issubclass(MailIdentityError, DeliveryError)


def test_message_carries_the_domain_and_no_address() -> None:
    with pytest.raises(MailIdentityError) as info:
        mail_identities("secret@mail.x.test", "Digest <other@Mail.x.test>")
    assert "mail.x.test" in str(info.value)
    assert "@" not in str(info.value)
    with pytest.raises(MailIdentityError) as blank:
        mail_identities("secret@", "mail.x.test")
    assert "@" not in str(blank.value)


def test_import_fence_is_stdlib_plus_errors() -> None:
    tree = ast.parse(Path(identity.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module or "")
    assert imported == {"__future__", "re", "dataclasses", "commishdesk.errors"}
