import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reviso.agent import Trace, chat, generate_report, packet, _numbers_supported
from reviso.analysis import build_report, discover
from reviso.data import Store, parse_csv
from reviso.exploration import date_value
from reviso.investigation import settings, bounded_result, size
from reviso.providers import Client, ProviderError
from reviso.structured import response_schema
from test_agent import FakeClient


class ExplorationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'test.sqlite')
        self.id=self.store.create_dataset('Single dataset')['id']
        output=io.StringIO(); writer=csv.writer(output)
        writer.writerow(['id','price','cost','group','rating','code','day','notes'])
        for i in range(1,31):
            writer.writerow([i,10000 if i==30 else i*10,5000 if i==30 else i*5,'North' if i%2 else 'South','Low' if i%2 else 'High',f'{i%3:03d}',f'2026-01-{i:02d}','' if i==3 else 'A description of a sample property with enough meaningful words for optional search.'])
        self.store.ingest(self.id,'Only file',output.getvalue().encode())
        self.store.configure(self.id,{'setup_complete':True,'identity':['id'],'target_column':'price','date_column':'day','dataset_type':'prices','important':['price']})
        FakeClient.answers=[];FakeClient.calls=[]

    def report(self):return build_report(self.store,self.id)
    def trace(self):return Trace(self.store,self.id,'chat','ollama','qwen3:8b')

    def test_complete_single_file_eda(self):
        report=self.report();e=report['exploration']
        self.assertEqual((report['summary']['versions'],e['records'],len(e['fields'])),(1,30,8))
        self.assertEqual(e['fields']['notes']['missing'],1)
        self.assertEqual(e['fields']['price']['unusual_values'],1)
        self.assertEqual(e['fields']['price']['median'],155)
        self.assertEqual(e['fields']['code']['role'],'nominal')
        self.assertEqual(e['fields']['rating']['role'],'ordinal')
        self.assertEqual(e['fields']['id']['role'],'identifier')
        self.assertEqual(e['fields']['day']['role'],'datetime')
        self.assertEqual(e['time_summary']['valid_dates'],30)
        self.assertEqual(e['group_summaries'][0]['measure'],'price')
        self.assertEqual(e['relationships'][0]['pearson_r'],1)
        self.assertTrue(any(f['kind']=='record_screen' and f['metrics']['records']==1 for f in report['findings']))
        self.assertEqual(len(e['record_samples']),12)
        self.assertTrue(all(f['scope']=='snapshot' for f in report['findings']))

    def test_roles_and_category_orders_are_user_controlled(self):
        self.store.configure(self.id,{'column_roles':{'price':'nominal'},'category_orders':{'rating':['Low','Medium']}})
        r=self.report();p=r['exploration']['fields']['price']
        self.assertEqual(p['role'],'nominal');self.assertTrue(p['role_confirmed'])
        self.assertNotIn('unusual_values',p)
        f=next(f for f in r['findings'] if f['kind']=='unordered_labels')
        self.assertEqual(f['metrics']['unranked_records'],15)
        self.assertEqual(r['exploration']['group_summaries'],[])

    def test_domain_clarifications_and_suggestions(self):
        d=discover(self.store,self.id)
        self.assertEqual(d['domain_suggestions'][0]['type'],'prices')
        self.assertEqual(d['field_suggestions']['rating']['role'],'ordinal')
        self.assertIn('currency',d['follow_up_questions'][0])
        self.store.configure(self.id,{'dataset_type':'claims','row_meaning':'A service line','analysis_goal':'Look for repeated payments'})
        self.assertIn('service line',self.report()['clarifications'][0])

    def test_new_analysis_setup_is_validated(self):
        for config in ({'dataset_type':'random'},{'column_roles':{'missing':'nominal'}},{'column_roles':{'price':'random'}},
                       {'category_orders':{'rating':['Low','Low']}},{'date_column':'missing'},{'time_grain':'year'},
                       {'date_format':'guess'},{'row_meaning':'x'*1001}):
            with self.subTest(config=config),self.assertRaises(ValueError):self.store.configure(self.id,config)

    def test_ambiguous_dates_require_explicit_format(self):
        self.assertIsNone(date_value('03/04/2026'))
        self.assertEqual(date_value('03/04/2026','%d/%m/%Y').month,4)
        self.assertEqual(date_value('03/04/2026','%m/%d/%Y').month,3)
        self.assertIsNone(date_value('2026-02-30'))

    def test_empty_and_repeated_rows(self):
        other=self.store.create_dataset('Repeated')['id']
        self.store.ingest(other,'one',b'id,value\n1,\n1,\n,\n')
        self.store.configure(other,{'setup_complete':True,'identity':['id']})
        r=build_report(self.store,other)
        self.assertEqual(next(f for f in r['findings'] if f['kind']=='repeated_records')['metrics']['extra_copies'],1)
        self.assertEqual(next(f for f in r['findings'] if f['kind']=='identity_current')['metrics']['missing_records'],1)
        empty=self.store.create_dataset('Empty')['id'];self.store.ingest(empty,'empty',b'column\n')
        self.store.configure(empty,{'setup_complete':True})
        self.assertEqual(build_report(self.store,empty)['exploration']['record_samples'],[])

    def test_chat_before_setup_builds_evidence_and_can_clarify(self):
        self.store.configure(self.id,{'setup_complete':False,'dataset_type':'unknown'})
        FakeClient.answers=[{'answer':'Does each row represent a property sale, and which outcome matters?','evidence_ids':[]}]
        result=chat(self.store,self.id,'Help me explore',self.trace(),'ollama','qwen3:8b',FakeClient)
        self.assertTrue(self.store.report(self.id)['draft'])
        self.assertEqual(len(self.store.history(self.id,result['report_id'])),2)
        self.assertTrue(FakeClient.calls[0]['draft'])
        self.assertTrue(FakeClient.calls[0]['clarifications'])

    def test_single_file_sql_chat_and_profile_tool(self):
        FakeClient.answers=[{'tool':'profile','version':1,'field_contains':'price'},
                            lambda c:{'tool':'sql','sql':f'SELECT max(price) AS maximum FROM "{c["schema"][0]["table"]}" LIMIT 1'},
                            {'answer':'The highest recorded price is 10000. [Q2]','evidence_ids':['Q2']}]
        answer=chat(self.store,self.id,'Highest price?',self.trace(),'ollama','qwen3:8b',FakeClient)
        self.assertEqual(answer['evidence']['Q2']['result']['rows'],[[10000]])
        self.assertEqual(FakeClient.calls[1]['tool_results'][0]['result']['columns'],['price'])

    def test_single_file_generated_report_cites_exploration(self):
        FakeClient.answers=[{'plan':['Inspect record examples.'],'queries':[]},
                            {'summary':'There are 30 records.','insights':[{'evidence_ids':['E001'],'interpretation':'The file contains 30 records.','next_step':'Confirm what each row represents.'}]}]
        generate_report(self.store,self.id,self.trace(),'ollama','qwen3:8b',FakeClient)
        self.assertEqual(self.store.report(self.id)['ai']['insights'][0]['evidence_ids'],['E001'])

    def test_invalid_chat_reference_has_one_visible_recovery_attempt(self):
        FakeClient.answers=[{'answer':'There are 30 records. [E999]','evidence_ids':['E999']},
                            {'answer':'There are 30 records. [E001]','evidence_ids':['E001']}]
        trace=self.trace()
        answer=chat(self.store,self.id,'Record count?',trace,'ollama','qwen3:8b',FakeClient)
        self.assertEqual(answer['evidence_ids'],['E001'])
        self.assertEqual(sum(e['kind']=='answer_rejected' for e in trace.payload['events']),1)
        self.assertIn('E001',FakeClient.calls[-1]['validation_feedback']['available_evidence_ids'])
        self.assertNotIn('E999',self.store.history(self.id,answer['report_id'])[-1]['content'])

    def test_report_correction_preserves_rejection_and_publishes_only_checked_text(self):
        FakeClient.answers=[{'plan':[],'queries':[]}, {'summary':'999999 records.','insights':[]},
                            {'summary':'The source has 30 records.','insights':[{'evidence_ids':['E001'],'interpretation':'The file was profiled.','next_step':'Confirm row meaning.'}]}]
        trace=self.trace()
        generate_report(self.store,self.id,trace,'ollama','qwen3:8b',FakeClient)
        self.assertEqual(self.store.report(self.id)['ai']['summary'],'The source has 30 records.')
        self.assertTrue(any(e['kind']=='interpretation_rejected' for e in trace.payload['events']))

    def test_missing_citation_array_gets_an_explicit_format_correction(self):
        FakeClient.answers=[{'answer':'There are 30 records. [E001]'}, {'answer':'There are 30 records. [E001]','evidence_ids':['E001']}]
        answer=chat(self.store,self.id,'Record count?',self.trace(),'ollama','qwen3:8b',FakeClient)
        self.assertEqual(answer['evidence_ids'],['E001'])
        self.assertIn('JSON array',FakeClient.calls[-1]['validation_feedback']['error'])

    def test_recovery_still_fails_on_unsupported_evidence(self):
        FakeClient.answers=[{'answer':'Changed. [E999]','evidence_ids':['E999']}]*2
        with self.assertRaisesRegex(ProviderError,'unavailable evidence'):
            chat(self.store,self.id,'Explain',self.trace(),'ollama','qwen3:8b',FakeClient)
        self.assertEqual(len(FakeClient.calls),2)
        self.assertEqual(self.store.history(self.id,self.store.report(self.id)['id']),[])

    def test_effort_changes_actual_report_query_budget(self):
        plan={'plan':[],'queries':[{'sql':'SELECT 1 AS checked LIMIT 1','purpose':'Inspect'}]*3}
        FakeClient.answers=[plan]
        with self.assertRaisesRegex(ProviderError,'query budget'):
            generate_report(self.store,self.id,self.trace(),'ollama','qwen3:8b',FakeClient,{'effort':'low'})
        FakeClient.answers=[plan,{'summary':'Measurements are available.','insights':[]}]
        generate_report(self.store,self.id,self.trace(),'ollama','qwen3:8b',FakeClient,{'effort':'high','context_window':32768})
        self.assertEqual(len(self.store.report(self.id)['ai']['query_results']),3)

    def test_evidence_packet_is_bounded_and_contains_record_examples(self):
        r=self.report();execution=settings('ollama','qwen3:8b')
        p=packet(r,execution)
        self.assertLess(size(p),execution['input_bytes'])
        self.assertTrue(any(f['kind']=='record_examples' and f['examples'] for f in p['findings']))
        self.assertEqual(p['config']['dataset_type'],'prices')
        self.assertLess(size(packet(r,settings('ollama',options={'context_window':8192}))),8192)

    def test_file_content_protocol(self):
        with self.assertRaisesRegex(ValueError,'binary'):parse_csv(b'x\n\x00\x01\n')
        with self.assertRaisesRegex(ValueError,'20,000'):parse_csv(('x\n'+'x'*20001).encode())
        self.assertEqual(parse_csv(b'code\n001\n')[1][0]['code'],'001')


class EffortTests(unittest.TestCase):
    def test_numeric_guard_allows_direct_decimal_rounding_without_new_counts(self):
        evidence={'missing_pct':3.333,'records':30}
        _numbers_supported('3.33% missing; 30.0 records. [E001]',evidence)
        for claim in ('3.34% missing','3 records','999 records','33.33% missing'):
            with self.subTest(claim=claim),self.assertRaises(ProviderError):_numbers_supported(claim,evidence)
        with self.assertRaises(ProviderError):_numbers_supported('999 records',{'id':'F999'})
        _numbers_supported('1000000 units',{'maximum':1e6})

    def test_local_contract_requires_citation_array_and_limits_ids(self):
        context={'response_kind':'chat','remaining_tools':0,'findings':[{'id':'E001'}],'tool_results':[{'id':'Q1','result':{'rows':[[30]]}}]}
        schema=response_schema(context)
        self.assertIn('evidence_ids',schema['required'])
        self.assertEqual(schema['properties']['evidence_ids']['items']['enum'],['E001','Q1'])
        self.assertFalse(schema['additionalProperties'])
        context['remaining_tools']=2
        self.assertEqual(len(response_schema(context)['oneOf']),3)
        context['semantic_index']={'ready':True}
        self.assertEqual(len(response_schema(context)['oneOf']),4)

    def test_local_generation_sends_schema_not_only_json_mode(self):
        c=Client('ollama','qwen3:8b')
        with patch.object(c,'request',side_effect=[{'capabilities':['completion']},{'message':{'content':'{"answer":"Which goal matters?","evidence_ids":[]}'}}]) as request:
            c.generate('Return JSON.',{'response_kind':'chat','remaining_tools':0,'findings':[{'id':'E001'}]})
        payload=request.call_args.args[1]
        self.assertEqual(payload['format']['properties']['evidence_ids']['type'],'array')
        self.assertEqual(payload['options']['temperature'],0)

    def test_auto_low_for_local_small_and_high_for_capable_cloud(self):
        for provider,model,expected in [('ollama','qwen3:8b','low'),('gemini','gemini-2.5-flash-lite','low'),('openai','gpt-5','high')]:
            self.assertEqual(settings(provider,model)['effort'],expected)
        self.assertEqual(settings('openai','gpt-5',{'context_window':8192})['effort'],'low')
        self.assertEqual(settings('ollama','qwen3:8b',{'effort':'high','context_window':32768})['chat_tools'],6)
        with self.assertRaisesRegex(ValueError,'32,768'):settings('ollama',options={'effort':'high','context_window':16384})
        for options in ({'effort':'max'},{'context_window':True},{'context_window':100}):
            with self.assertRaises(ValueError):settings('ollama',options=options)

    def test_tool_results_expose_context_truncation(self):
        compact=bounded_result({'columns':['description'],'rows':[['x'*1000]]*20,'truncated':False},2000)
        self.assertEqual(len(compact['rows']),1)
        self.assertIn('prefix',compact['context_note'])
        self.assertLessEqual(size(compact),2000)

    def test_native_reasoning_only_for_supported_models(self):
        for provider,model,supported in [('openai','gpt-5',True),('openai','gpt-4o',False),('gemini','gemini-2.5-flash',True),('groq','openai/gpt-oss-20b',True),('groq','llama-3.3-70b-versatile',False)]:
            with self.subTest(provider=provider,model=model):
                c=Client(provider,model,settings={'api_key':'test','model':model})
                c.configure_investigation(settings(provider,model,{'effort':'high','context_window':32768}))
                result={'output':[{'content':[{'type':'output_text','text':'{"ok":true}'}]}]} if provider=='openai' else {'choices':[{'message':{'content':'{"ok":true}'}}]}
                with patch.object(c,'request',return_value=result) as request:
                    self.assertTrue(c.generate('Return JSON.',{})['ok'])
                payload=request.call_args.args[1]
                self.assertEqual('reasoning' in payload or 'reasoning_effort' in payload,supported)
                self.assertEqual(payload.get('max_output_tokens',payload.get('max_completion_tokens')),8000)

    def test_ollama_local_effort_sets_context_and_capability_checked_thinking(self):
        for capabilities in (['completion','thinking'],['completion']):
            c=Client('ollama','qwen3:8b');c.configure_investigation(settings('ollama',options={'effort':'high','context_window':32768}))
            with patch.object(c,'request',side_effect=[{'capabilities':capabilities},{'message':{'content':'{"ok":true}'}}]) as request:
                c.generate('Return JSON.',{})
            payload=request.call_args.args[1]
            self.assertEqual(payload['options']['num_ctx'],32768)
            self.assertEqual(payload.get('think'),'thinking' in capabilities if 'thinking' in capabilities else None)

    def test_context_overflow_fails_without_a_network_call(self):
        c=Client('ollama','qwen3:8b');c.configure_investigation(settings('ollama',options={'context_window':4096}))
        with patch.object(c,'request') as request,self.assertRaisesRegex(ProviderError,'context budget'):
            c.generate('System',{'data':'x'*5000})
        request.assert_not_called()
