import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from reviso.providers import Client, ProviderError, connections


class Response:
    def __init__(self,value): self.value=value
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self,*args): return json.dumps(self.value).encode()


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'OPENAI_API_KEY':'test-secret','GEMINI_API_KEY':'test-secret','GROQ_API_KEY':'test-secret'}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)

    def test_connections_never_return_keys_or_assume_free_tier(self):
        value = json.dumps(connections())
        self.assertNotIn('test-secret',value)
        self.assertIn('not inferred',value)

    def test_openai_responses_contract(self):
        with patch('urllib.request.urlopen',return_value=Response({'output':[{'type':'reasoning','content':[]},{'content':[{'type':'output_text','text':'{"answer":"ok"}'}]}],'usage':{'input_tokens':3}})) as network:
            result = Client('openai','test-model').generate('instruction',{'question':'test'})
            request = network.call_args[0][0]
            payload = json.loads(request.data)
            self.assertEqual(request.full_url,'https://api.openai.com/v1/responses')
            self.assertFalse(payload['store'])
            self.assertEqual(result,{'answer':'ok'})

    def test_gemini_and_groq_chat_contracts(self):
        for provider, base in (('gemini','https://generativelanguage.googleapis.com/v1beta/openai'),('groq','https://api.groq.com/openai/v1')):
            with self.subTest(provider=provider), patch('urllib.request.urlopen',return_value=Response({'choices':[{'message':{'content':'{"ok":true}'}}]})) as network:
                self.assertTrue(Client(provider,'test-model').generate('instruction',{})['ok'])
                request=network.call_args[0][0]
                self.assertEqual(request.full_url,base+'/chat/completions')
                self.assertEqual(json.loads(request.data)['messages'][0]['role'],'system')

    def test_no_missing_credentials_or_invented_model_default(self):
        with self.assertRaisesRegex(ProviderError,'model ID'):
            Client('openai')
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(ProviderError,'OPENAI_API_KEY'):
            Client('openai','test')

    def test_quota_failure_is_recorded_and_body_is_not_exposed(self):
        error=urllib.error.HTTPError('https://example.test',429,'rate limit',{'Retry-After':'0'},io.BytesIO(b'test-secret private dataset echoed'))
        events=[]
        with patch('urllib.request.urlopen',side_effect=error),patch('time.sleep'):
            with self.assertRaisesRegex(ProviderError,'quota') as raised:
                Client('groq','test',lambda k,d:events.append((k,d))).generate('instruction',{})
        self.assertNotIn('test-secret',str(raised.exception))
        self.assertEqual(len(events),2)
        self.assertTrue(all(e[1]['status']=='failed' for e in events))

    def test_malformed_structured_answer_rejected(self):
        for text in ('not json','[]'):
            with patch('urllib.request.urlopen',return_value=Response({'choices':[{'message':{'content':text}}]})),self.assertRaises(ProviderError):
                Client('gemini','test').generate('instruction',{})

    def test_embedding_results_are_ordered_and_validated(self):
        with patch('urllib.request.urlopen',return_value=Response({'data':[{'index':1,'embedding':[0,1]},{'index':0,'embedding':[1,0]}]})):
            self.assertEqual(Client('openai','test').embed(['a','b'],'test-embed'),[[1,0],[0,1]])
        with patch('urllib.request.urlopen',return_value=Response({'data':[{'index':0,'embedding':[float('nan')]}]})),self.assertRaises(ProviderError):
            Client('openai','test').embed(['a'],'test-embed')

    def test_groq_embedding_requires_separate_connection(self):
        with self.assertRaisesRegex(ProviderError,'separate'):
            Client('groq','test').embed(['text'],'test-embed')


if __name__ == '__main__':
    unittest.main()
