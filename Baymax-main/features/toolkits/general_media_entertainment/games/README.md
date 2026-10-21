# Games

`trivia.py` fetches five Open Trivia DB questions and returns one per call, retaining
the rest locally. Gemini asks the question and waits before revealing the answer.
No user profile or conversation text is sent. No new dependencies are required.

Enable Public information plus Discovery; disable either to hide the game tool.
See [conversation tools](../../CONVERSATION.md) for caching, provider limits and
verification. Offline coverage is in `tests/test_toolkit_conversation.py`.
