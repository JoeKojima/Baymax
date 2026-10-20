import json
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest
from features.memory_palace.notes import compact_note, observed_date
from features.memory_palace.runtime_context import graph_lines, legacy_line


class CompactNotesTests(unittest.TestCase):
    def test_real_injector_omits_oversized_and_duplicate_notes(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'realtime_gemini_10.py').read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='_MemoryInjector')
        state={'person_id':'a','name':'Alex','generation':1}
        ns={'asyncio':asyncio,'time':__import__('time'),'_current_memory_context':lambda:state,'_face_id':None}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'runtime','exec'),ns)
        session=SimpleNamespace(send_client_content=AsyncMock())
        injector=ns['_MemoryInjector'](session);injector.owner_generation=1
        asyncio.run(injector._send([('large','x'*901),('kept','y'*600),('same','y'*600),('overflow','z'*400)]))
        sent=session.send_client_content.call_args.kwargs
        self.assertFalse(sent['turn_complete'])
        text=sent['turns']['parts'][0]['text']
        self.assertEqual(text.split('\n',1)[1], 'y'*600)
        self.assertNotIn('xxxx',text)
        self.assertNotIn('zzzz',text)
        self.assertEqual(set(injector.sent),{'kept'})

    def test_subject_is_only_removed_for_a_single_explicit_owner(self):
        text='Alex likes piano but does not like jazz.'
        self.assertEqual(compact_note(text,['Alex']), 'likes piano but does not like jazz.')
        self.assertEqual(compact_note(text,['Alex','Sam']), text)
        self.assertEqual(compact_note('Sam likes piano.',['Alex']), 'Sam likes piano.')
        self.assertEqual(compact_note('Alex Smith likes piano.',['Alex']), 'Alex Smith likes piano.')

    def test_dates_are_short_but_missing_years_are_not_invented(self):
        self.assertEqual(compact_note('Visit on Sunday, October 18, 2026.'),'Visit on 2026-10-18.')
        self.assertEqual(compact_note('Visit on Sunday, October 18.'),'Visit on Oct 18.')
        self.assertIsNone(observed_date(None))
        self.assertIsNone(observed_date(0))
        self.assertIn('February 31',compact_note('February 31, 2026'))

    def test_old_summary_keeps_topics_and_original_source_is_not_modified(self):
        source='On Sunday, October 18, Alex talked with Ember about piano and work.'
        self.assertEqual(compact_note(source,['Alex'],'conversation'),'Topics: piano and work.')
        self.assertTrue(source.startswith('On Sunday'))

    def test_compact_graph_recall_retains_polarity_qualifiers_without_quotes(self):
        row={'id':'1','kind':'learning','value':'beginner piano','status':'self-reported negative',
             'quote':'I am not a beginner piano student anymore.','last_seen':1792368000}
        lines=graph_lines([row,row],{'name':'Alex'})
        self.assertEqual(len(lines),1)
        payload=json.loads(lines[0][1])
        self.assertEqual(payload['learning'],'beginner piano')
        self.assertTrue(payload['not'])
        self.assertRegex(payload['seen'],r'^\d{4}-\d{2}-\d{2}$')
        self.assertNotIn('quote',payload)
        self.assertEqual(row['quote'],'I am not a beginner piano student anymore.')

    def test_context_is_bounded_without_cutting_a_fact_in_half(self):
        row={'id':'a','kind':'preference','value':'x'*1000,'status':'self-reported','last_seen':0}
        self.assertEqual(graph_lines([row],{'name':'Alex'}),[])

    def test_payload_reduction_and_legacy_owner_metadata(self):
        rows=[{'id':str(i),'kind':'preference','value':value,'status':'self-reported',
               'quote':'I really enjoy '+value+' when I have some free time to myself.',
               'last_seen':1792368000} for i,value in enumerate(('classical piano','fresh grapes','surfing'))]
        old='\n'.join('[SELF-REPORTED GRAPH MEMORY] '+json.dumps(dict(memory_profile='Alex',kind=r['kind'],value=r['value'],status=r['status'],quote=r['quote'],observed_at=r['last_seen'])) for r in rows)
        new='\n'.join(line for _,line in graph_lines(rows,{'name':'Alex'}))
        self.assertLess(len(new),len(old)*.6)
        m={'memory':'Alex prefers quiet music.','people':['Alex'],'time':1792368000}
        payload=json.loads(legacy_line(m,{'name':'Alex'}))
        self.assertEqual(payload['note'],'prefers quiet music.')
        self.assertEqual(m['memory'],'Alex prefers quiet music.')
