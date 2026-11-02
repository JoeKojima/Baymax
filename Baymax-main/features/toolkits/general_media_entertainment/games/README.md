# Games

`trivia.py` fetches five Open Trivia DB questions and returns one per call, retaining
the rest locally. Gemini asks the question and waits before revealing the answer.
No user profile or conversation text is sent. No new dependencies are required.

## Brain games (`brain.py`)

Spoken games for fun, played **only when the person asks to play**. Ember never
offers one at startup or in a greeting; it may suggest one if asked what to do or
told they are bored.

- **Games:** word association, twenty questions (Ember thinks of something, or
  asks the questions), categories ("name 4 fruits starting with B"), remembering
  a list (4/5/6 words for easy/medium/hard), story recall, riddles, and word
  puzzles (rhymes, opposites, unscrambling).
- **Tool:** `brain_game`, with `action` start / next / result / score / end, plus
  optional `game`, `difficulty` and `correct`. Each round comes from local word
  lists and is not repeated until that list is used up. The score lasts until the
  game ends or Ember restarts. Answers come back hidden, so Ember waits before
  revealing them.
- **Prompt (`[BRAIN GAMES]`):** warm and encouraging, never condescending; hints;
  difficulty adjusted to the person; stop or switch any time; never called a test,
  no comments on memory, never a medical or cognitive judgement. The lists are
  English; Ember translates and plays in the person's language.
- **Inputs, outputs and data flows:** the tool arguments in, round text and score
  out. No network, provider, storage, dependency or device use.
- **Enable/disable:** part of Discovery (on by default). `EMBER_DISCOVERY_ENABLED=0`
  or `BAYMAX_TOOLKIT_ENABLED=0` removes the tool and its prompt section.
- **Tests:** `tests/test_toolkit_brain_games.py` covers every game, no repeats,
  list lengths, hidden answers, scoring, invalid input, enablement, routing and
  the no-network check.
- **Live checks pending:** that Ember waits to be asked, its tone, how well it
  judges answers in other languages, and pacing for list recall.

## Enabling trivia

Enable Public information plus Discovery; disable either to hide the game tool.
See [conversation tools](../../CONVERSATION.md) for caching, provider limits and
verification. Offline coverage is in `tests/test_toolkit_conversation.py`.
