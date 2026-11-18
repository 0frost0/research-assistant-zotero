import copy
from pathlib import Path
import tempfile
import unittest
from research_assistant.core.service_config import ServiceConfigStore, chat_base_url
from research_assistant.core.service_context import llm_config, service_context, stored_service_context


class ChatBaseURLTests(unittest.TestCase):
    def test_copied_endpoint_keeps_the_provider_prefix(self):
        self.assertEqual(chat_base_url('https://api.deepseek.com/chat/completions/'), 'https://api.deepseek.com')
        self.assertEqual(chat_base_url('https://provider.invalid/prefix/v1/chat/completions'),
                         'https://provider.invalid/prefix/v1')
        self.assertEqual(chat_base_url('https://provider.invalid/v1'), 'https://provider.invalid/v1')

    def test_runtime_normalization_does_not_mutate_task_config_or_touch_embedding(self):
        context = {'connection': 'test', 'config': {
            'llm': {'base_url': 'https://provider.invalid/v1/chat/completions', 'model': 'test', 'api_key': 'secret'},
            'embedding': {'base_url': 'http://127.0.0.1:30001/v1/embeddings'}}}
        before = copy.deepcopy(context)
        token = service_context.set(context)
        try:
            self.assertEqual(llm_config()['base_url'], 'https://provider.invalid/v1')
            self.assertEqual(context, before)
        finally:
            service_context.reset(token)

    def test_saving_an_equivalent_endpoint_preserves_existing_keys(self):
        original = {'llm': {'base_url': 'https://provider.invalid/v1/chat/completions',
                            'model': 'test', 'api_key': 'secret'},
                    'embedding': {'base_url': 'http://127.0.0.1:30001/v1',
                                  'model': 'vectors', 'api_key': 'vector-secret'}}
        with tempfile.TemporaryDirectory() as directory:
            store = ServiceConfigStore(Path(directory))
            store._write({'test': original})
            update = copy.deepcopy(original)
            update['llm']['api_key'] = update['embedding']['api_key'] = ''
            result = store.save('test', update)
            self.assertEqual(result['llm']['base_url'], 'https://provider.invalid/v1')
            self.assertEqual(result['llm']['api_key'], 'secret')
            self.assertEqual(result['embedding'], original['embedding'])

    def test_cli_profile_is_restored_even_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ServiceConfigStore(root)._write({'cli': {'llm': {
                'base_url': 'https://provider.invalid/v1', 'model': 'saved-model', 'api_key': 'secret'}}})
            previous = service_context.get()
            with self.assertRaises(RuntimeError):
                with stored_service_context(root, 'cli'):
                    self.assertEqual(llm_config()['model'], 'saved-model')
                    raise RuntimeError('fixture')
            self.assertIs(service_context.get(), previous)
            with self.assertRaises(ValueError):
                with stored_service_context(root, 'missing'):
                    self.fail('Missing profile must not silently use environment credentials')
