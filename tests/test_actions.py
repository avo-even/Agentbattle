"""Exhaustive tests for the action parser (spec §5.4, §12)."""

import pytest

from actions import format_action, parse_action, sanitize_name, truncate


def test_clean_offer():
    r = parse_action('I take 60.\n{"offer": [60, 40]}')
    assert r.action == {"type": "offer", "split": [60, 40]}
    assert r.text == "I take 60."


def test_clean_accept():
    r = parse_action('Deal.\n{"accept": true}')
    assert r.is_accept
    assert r.text == "Deal."


def test_fenced_action():
    r = parse_action('Here you go.\n```json\n{"offer": [55, 45]}\n```')
    assert r.split == [55, 45]
    assert "```" not in r.text
    assert r.text == "Here you go."


def test_action_mid_prose():
    r = parse_action('My move {"offer": [70, 30]} — take it or leave it.')
    assert r.split == [70, 30]
    assert r.text == "My move — take it or leave it."


def test_multiple_blobs_takes_first_valid():
    reply = 'One {"offer": [70, 30]} then {"offer": [50, 50]}'
    r = parse_action(reply)
    assert r.split == [70, 30]
    assert "{" not in r.text  # both blobs stripped from the display text


def test_first_invalid_then_valid():
    reply = '{"offer": [70, 40]} sorry, I meant {"offer": [70, 30]}'
    r = parse_action(reply)
    assert r.split == [70, 30]
    assert r.invalid  # the bad one was logged


def test_invalid_sum_is_dropped():
    r = parse_action('{"offer": [70, 40]}')
    assert r.action is None
    assert "sums to 110" in r.invalid[0]


def test_non_integer_offer_dropped():
    r = parse_action('{"offer": [60.5, 39.5]}')
    assert r.action is None
    assert r.invalid


def test_float_that_is_whole_is_accepted():
    assert parse_action('{"offer": [60.0, 40.0]}').split == [60, 40]


def test_negative_offer_dropped():
    r = parse_action('{"offer": [110, -10]}')
    assert r.action is None


def test_zero_is_legal():
    assert parse_action('{"offer": [100, 0]}').split == [100, 0]


def test_no_action():
    r = parse_action("Let's be reasonable about this.")
    assert r.action is None
    assert r.invalid == []
    assert r.text == "Let's be reasonable about this."


def test_empty_reply():
    r = parse_action("")
    assert r.action is None and r.text == ""
    r = parse_action(None)
    assert r.action is None and r.text == ""


def test_garbage_never_raises():
    for junk in ['{{{{', '{"offer":', '}{', '{"offer": [1,2,3,4]}', '{"offer": null}', "{'offer': [50, 50]}"]:
        parse_action(junk)  # must not raise
    assert parse_action("{'offer': [50, 50]}").split == [50, 50]


def test_prose_with_braces_not_treated_as_action():
    r = parse_action("Set {x} to whatever you like.")
    assert r.action is None
    assert r.text == "Set {x} to whatever you like."


def test_accept_string_form():
    assert parse_action('{"accept": "yes"}').is_accept
    assert parse_action('{"action": "accept"}').is_accept


def test_accept_false_is_not_an_accept():
    r = parse_action('{"accept": false}')
    assert r.action is None


def test_dict_offer_forms():
    assert parse_action('{"offer": {"me": 65, "them": 35}}').split == [65, 35]
    assert parse_action('{"offer": {"self": 20, "opponent": 80}}').split == [20, 80]


def test_string_offer_forms():
    assert parse_action('{"offer": "60/40"}').split == [60, 40]
    assert parse_action('{"offer": 70}').split == [70, 30]


def test_action_verb_with_split_key():
    assert parse_action('{"action": "offer", "split": [51, 49]}').split == [51, 49]


def test_nested_json_is_scanned_correctly():
    r = parse_action('preamble {"meta": {"a": 1}, "offer": [50, 50]} tail')
    assert r.split == [50, 50]


def test_custom_pot():
    assert parse_action('{"offer": [10, 40]}', pot=50).split == [10, 40]
    assert parse_action('{"offer": [10, 40]}', pot=100).action is None


# --- truncation / sanitization ---------------------------------------------

def test_truncate():
    text, cut = truncate("a" * 400, 300)
    assert cut and len(text) == 301  # 300 chars + ellipsis
    text, cut = truncate("short", 300)
    assert not cut and text == "short"


def test_sanitize_name():
    assert sanitize_name("  The  Guac \n Squad ") == "The Guac Squad"
    assert sanitize_name("<script>alert(1)</script>") == "scriptalert(1)/script"
    assert sanitize_name("x" * 100) == "x" * 28
    assert sanitize_name("") == ""
    assert sanitize_name("bad\x00name") == "badname"


def test_format_action_roundtrip():
    assert format_action({"type": "offer", "split": [60, 40]}) == '{"offer": [60, 40]}'
    assert format_action({"type": "accept"}) == '{"accept": true}'
    assert format_action(None) == ""
    assert parse_action(format_action({"type": "offer", "split": [1, 99]})).split == [1, 99]
