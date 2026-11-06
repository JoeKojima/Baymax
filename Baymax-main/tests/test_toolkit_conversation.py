import json
import os
import unittest
from unittest.mock import patch

from features.toolkits.experimental import Toolkit, DECLARATIONS, GROUPS
from features.toolkits.transport import PublicClient, HOSTS


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"EMBER_PUBLIC_TOOLKIT": "1", "BAYMAX_TOOLKIT_ENABLED": "1", "EMBER_PUBLIC_TOOL_GROUPS": "discovery"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = []
        self.payload = {}
        def fetch(url, params):
            self.calls.append((url, params))
            if isinstance(self.payload, Exception):
                raise self.payload
            return json.dumps(self.payload).encode()
        self.tool = Toolkit(client=PublicClient(fetch=fetch))

    def call(self, name, **args):
        return self.tool.execute('public_' + name, args)

    def test_trivia_batch_consumed_without_refetch_and_cooldown(self):
        self.payload = {"response_code": 0, "results": [
            {"question": f"Q{i} &amp;?", "correct_answer": "A", "incorrect_answers": ["B"]} for i in range(5)]}
        for i in range(5):
            self.assertEqual(self.call('trivia')['question'], f'Q{i} &?')
        self.assertEqual(len(self.calls), 1)
        self.assertIn('error', self.call('trivia'))
        self.assertEqual(len(self.calls), 1)

    def test_trivia_rate_limit_not_a_question(self):
        self.payload = {"response_code": 5, "results": []}
        self.assertIn('error', self.call('trivia'))

    def test_shows_ambiguity_and_html(self):
        self.payload = [{"show": {"id": i, "name": "Test", "summary": "<p>A &amp; B</p>"}} for i in (1, 2)]
        result = self.call('shows', query='Test')
        self.assertEqual(result['shows'][0]['summary'], 'A & B')
        self.assertEqual(len(self.calls), 1)

    def test_unique_show_cast_and_schedule(self):
        responses = [
            [{"show": {"id": 5, "name": "Example"}}],
            {"_embedded": {"nextepisode": {"name": "Next", "airstamp": "2026-10-22T20:00:00Z"}}},
            [{"person": {"name": "Actor"}}]]
        self.tool.client.fetch = lambda url, params: json.dumps(responses.pop(0)).encode()
        show = self.call('shows', query='Example')['shows'][0]
        self.assertEqual(show['cast'], ['Actor'])
        self.assertEqual(show['next_episode']['name'], 'Next')

    def test_poem_is_bounded_and_path_encoded(self):
        self.payload = [{"title": "Title", "author": "Author", "lines": ['line'] * 20}]
        poem = self.call('poem', author='A/B')['poems'][0]
        self.assertTrue(poem['excerpt'])
        self.assertEqual(len(poem['lines']), 16)
        self.assertIn('A%2FB', self.calls[0][0])

    def test_poem_by_title_prefers_the_named_author(self):
        self.payload = [{"title": "The Raven", "author": "Someone Else", "lines": ['x']},
                        {"title": "The Raven", "author": "Edgar Allan Poe", "lines": ['Once upon a midnight dreary']}]
        poem = self.call('poem', title='The Raven', author='Poe')['poems'][0]
        self.assertEqual(poem['author'], 'Edgar Allan Poe')
        self.assertIn('title/The%20Raven/', self.calls[0][0])

    def test_radio_is_discovery_only(self):
        self.payload = [{"name": "Jazz", "url_resolved": "http://localhost/private"}]
        result = self.call('radio', genre='jazz', country_code='us')
        self.assertEqual(self.calls[0][1]['countrycode'], 'US')
        self.assertNotIn('url_resolved', result['stations'][0])
        self.assertIn('cannot play', result['notice'])

    def test_meals_ingredients_and_empty_results(self):
        self.payload = {'meals': [{'strMeal': 'Soup', 'strIngredient1': 'Water', 'strMeasure1': '1 cup', 'strInstructions': 'Boil.'}]}
        self.assertEqual(self.call('meals', query='soup')['meals'][0]['ingredients'], ['1 cup Water'])
        self.payload = {'meals': None}
        self.assertEqual(self.call('meals', query='unknown')['meals'], [])

    def test_invalid_inputs_do_not_fetch(self):
        for name, args in [('shows', {'query': ''}), ('poem', {'author': []}),
                           ('radio', {'genre': 'jazz', 'country_code': 'USA'}),
                           ('meals', {'query': 'x' * 161}), ('trivia', {'url': 'https://example.org'})]:
            with self.subTest(name=name):
                self.assertIn('error', self.call(name, **args))
        self.assertEqual(self.calls, [])

    def test_all_five_fail_safely_and_disable_without_fetch(self):
        cases = [('trivia', {}), ('shows', {'query': 'Test'}), ('poem', {}),
                 ('radio', {'genre': 'jazz'}), ('meals', {'query': 'soup'})]
        for payload in (RuntimeError('secret failure'), ['malformed']):
            self.payload = payload
            for name, args in cases:
                with self.subTest(name=name, payload=repr(payload)):
                    result = self.call(name, **args)
                    self.assertIn('error', result)
                    self.assertNotIn('secret failure', str(result))
        self.calls.clear()
        with patch.dict(os.environ, {'EMBER_PUBLIC_TOOLKIT': '0'}):
            for name, args in cases:
                self.assertIn('error', self.call(name, **args))
        self.assertEqual(self.calls, [])

    def test_registry_and_hosts(self):
        names = {d['name'] for d in DECLARATIONS}
        self.assertTrue(GROUPS['discovery'] <= names)
        self.assertTrue({'opentdb.com', 'api.tvmaze.com', 'poetrydb.org',
                         'de1.api.radio-browser.info', 'www.themealdb.com'} <= HOSTS)


if __name__ == '__main__':
    unittest.main()
