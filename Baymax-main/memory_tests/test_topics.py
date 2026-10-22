from ember_topics import TopicMatcher, prediction_context


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


def test_filler_unknown_aliases_and_learning_cap():
    matcher = TopicMatcher({"Dr Patel": "appointments"}, max_terms=4)
    assert matcher.feed("I was wondering, tell me about xyzzy. ", 1) == ()
    assert matcher.feed("Dr Pat", 2) == ()
    assert matcher.feed("el cancelled. ", 3) == ("appointments",)
    matcher.learn(["alpha bravo charlie delta echo foxtrot I was wondering tell me"])
    assert matcher.learned_count == 4
    matcher.reset()
    assert matcher.feed("alpha ", 4) == ()
    assert matcher.feed("foxtrot ", 5) == ("foxtrot",)


def test_negation_and_snapshot_context_are_preserved():
    current = "Sarah is not visiting; her appointment was cancelled."
    messages = [("robot", str(i) + "x"*500) for i in range(8)]
    context = prediction_context(current, messages)
    assert len(context) <= 2000
    assert context.endswith("patient: " + current)
    assert "0xxx" not in context
