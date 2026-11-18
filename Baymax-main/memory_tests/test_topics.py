import pytest

from ember_topics import TopicMatcher, parse_summaries


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
    matcher = TopicMatcher({"Dr Patel": "appointments"})
    assert matcher.feed("I was wondering, tell me about Sarah xyzzy. ", 1) == ()
    assert matcher.feed("Dr Pat", 2) == ()
    assert matcher.feed("el cancelled. ", 3) == ("appointments",)


def test_summary_alias_tags_and_untagged_facts():
    assert parse_summaries('[{"summary":"Chess.","tags":["CHESS","chess","unknown"]},'
                           '{"summary":"Fact.","tags":[]}]', {"club": "chess"}) == [
        ("Chess.", ["chess"]), ("Fact.", [])]


@pytest.mark.parametrize("text", ["not json", "{}", '[{"tags": ["family"]}]', '[{"summary": 5}]'])
def test_malformed_summaries_are_rejected(text):
    with pytest.raises(ValueError):
        parse_summaries(text)
