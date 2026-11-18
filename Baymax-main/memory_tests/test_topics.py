import json

import pytest

from ember_topics import TopicMatcher, parse_summaries, topic_vocabulary


def test_split_word_boundary_debounce_and_no_prefix():
    matcher = TopicMatcher({"Sarah": "family"})
    assert matcher.feed("Sa", 1) == ()
    assert matcher.feed("rah", 1.05) == ()
    assert matcher.poll(1.249) == ()
    assert matcher.poll(1.251) == ("family",)
    assert matcher.feed(" is visiting ", 1.3) == ("visits",)
    assert matcher.feed("Sarah Sarah ", 2) == ()
    matcher.reset()
    assert matcher.feed("Sarahson ", 3) == ()
    assert matcher.feed("gardenia ", 4) == ()


def test_filler_unknown_words_and_phrase_alias():
    matcher = TopicMatcher({"Dr Patel": "appointments"})
    assert matcher.feed("I was wondering, tell me about Sarah xyzzy. ", 1) == ()
    assert matcher.feed("Dr Pat", 2) == ()
    assert matcher.feed("el cancelled. ", 3) == ("appointments",)


def test_summary_tags_share_vocabulary_normalize_and_preserve_untagged_facts():
    aliases = {"Sarah": " FAMILY ", "chess club": "chess"}
    assert "chess" in topic_vocabulary(aliases)
    records = [{"summary": " Sarah visits. ", "tags": [" FAMILY ", "family", "visits", "Sarah", 5]},
               {"summary": "Likes chess.", "tags": ["CHESS"]},
               {"summary": "Untyped fact.", "tags": "not a list"},
               {"summary": "Another fact.", "tags": ["unknown"]}, {"summary": "  "}]
    assert parse_summaries(json.dumps(records), aliases) == [
        ("Sarah visits.", ["family", "visits"]), ("Likes chess.", ["chess"]),
        ("Untyped fact.", []), ("Another fact.", [])]
    assert parse_summaries("[]") == []
    matcher = TopicMatcher(aliases)
    assert matcher.feed("Sarah chess club ", 1) == ("chess", "family")


@pytest.mark.parametrize("text", ["not json", "{}", '[{"tags": ["family"]}]', '[{"summary": 5}]'])
def test_malformed_summaries_are_rejected(text):
    with pytest.raises(ValueError):
        parse_summaries(text)
