"""Latency paths use real calculation fixtures and a mocked API transport."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from replenishment import agent
from replenishment.demo import make_demo
from replenishment.engine import calculate_all


def message(text='Короткий ответ.'):
    return {'status': 'completed', 'output': [{'type': 'message', 'content': [
        {'type': 'output_text', 'text': text}]}]}


def function_call(arguments='{}', name='inventory_summary', call_id='call-1'):
    return {'output': [{'type': 'function_call', 'name': name, 'arguments': arguments,
                        'call_id': call_id, 'id': 'fc-'+call_id}]}


class AgentSpeedTests(unittest.TestCase):
    def setUp(self):
        self.dataset = make_demo()
        self.settings = copy.deepcopy(app.DEFAULTS)
        self.rows = calculate_all(self.dataset, self.settings)
        env = patch.dict('os.environ', {'OPENAI_API_KEY': 'test', 'OPENAI_MODEL': 'gpt-5-mini'}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def live(self, text='Посоветуй подход к закупке', **kwargs):
        return agent.ask_live(text, self.dataset, self.settings, self.rows, **kwargs)

    def test_short_answer_payload_preserves_model_and_limits_history(self):
        history = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': str(i)*2000}
                   for i in range(8)]
        payloads = []
        def transport(payload):
            payloads.append(copy.deepcopy(payload))
            return message()
        with patch.object(agent, 'remote_request', side_effect=transport):
            result = self.live(history=history)
        payload = payloads[0]
        self.assertEqual(payload['model'], 'gpt-5-mini')
        self.assertEqual(payload['reasoning'], {'effort': 'minimal'})
        self.assertEqual(payload['text'], {'verbosity': 'low'})
        self.assertEqual(payload['max_output_tokens'], 1600)
        self.assertFalse(payload['store'])
        self.assertEqual(len(payload['input']), 5)
        self.assertTrue(all(len(item['content']) == 1200 for item in payload['input'][:-1]))
        self.assertEqual(payload['input'][0]['content'], '4'*1200)
        self.assertEqual(result['metrics']['model_calls'], 1)

    def test_configured_other_model_is_not_overridden(self):
        with patch.dict('os.environ', {'OPENAI_MODEL': 'custom-model'}), \
                patch.object(agent, 'remote_request', return_value=message()) as remote:
            self.live()
        payload = remote.call_args.args[0]
        self.assertEqual(payload['model'], 'custom-model')
        self.assertNotIn('reasoning', payload)

    def test_repeated_calls_are_cached_and_third_round_requires_answer(self):
        payloads = []
        responses = iter([function_call(), function_call(call_id='call-2'), message()])
        def transport(payload):
            payloads.append(copy.deepcopy(payload))
            return next(responses)
        with patch.object(agent, 'remote_request', side_effect=transport) as remote, \
                patch.object(agent, 'call_tool', wraps=agent.call_tool) as call:
            result = self.live()
        self.assertEqual(remote.call_count, 3)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result['metrics']['cache_hits'], 1)
        self.assertEqual(result['metrics']['tool_calls'], 1)
        self.assertEqual(payloads[-1]['tool_choice'], 'none')
        self.assertEqual(result['answer'], 'Короткий ответ.')

    def test_even_misbehaving_transport_cannot_trigger_fourth_request(self):
        with patch.object(agent, 'remote_request', side_effect=[
                function_call(call_id=str(i)) for i in range(3)]) as remote:
            result = self.live()
        self.assertEqual(remote.call_count, 3)
        self.assertIn('Уточните', result['answer'])

    def test_bad_tool_arguments_are_reported_to_model_not_executed(self):
        for invalid in ('{invalid', '[]', 'null'):
            with self.subTest(arguments=invalid):
                payloads = []
                responses = iter([function_call(arguments=invalid), message()])
                def transport(payload):
                    payloads.append(copy.deepcopy(payload))
                    return next(responses)
                with patch.object(agent, 'remote_request', side_effect=transport), \
                        patch.object(agent, 'call_tool', wraps=agent.call_tool) as call:
                    self.live()
                self.assertEqual(call.call_count, 0)
                outputs = [entry for entry in payloads[-1]['input']
                           if entry.get('type') == 'function_call_output']
                self.assertIn('error', json.loads(outputs[-1]['output']))

    def test_incomplete_output_does_not_appear_as_complete_recommendation(self):
        response = message('Закажите 500 штук, потому что')
        response['status'] = 'incomplete'
        with patch.object(agent, 'remote_request', return_value=response):
            result = self.live()
        self.assertIn('не завершён', result['answer'])
        self.assertNotIn('500', result['answer'])

    def test_ui_shortcuts_need_no_remote_calls(self):
        for text in ('Какие позиции требуют срочного заказа?',
                     'Покажи разовые всплески продаж', 'Сценарий роста 20%'):
            with self.subTest(question=text), patch.object(agent, 'remote_request') as remote:
                result = agent.ask(text, self.dataset, self.settings, self.rows)
                remote.assert_not_called()
                self.assertEqual(result['mode'], 'calculated')
                self.assertEqual(result['metrics']['model_calls'], 0)
                self.assertTrue(result['answer'])

    def test_explicit_details_fall_through_to_language_model(self):
        with patch.object(agent, 'remote_request', return_value=message()) as remote:
            result = agent.ask('Подробно: какие позиции требуют срочного заказа?',
                               self.dataset, self.settings, self.rows)
        self.assertEqual(result['mode'], 'live')
        payload = remote.call_args.args[0]
        self.assertEqual(payload['reasoning'], {'effort': 'low'})
        self.assertEqual(payload['max_output_tokens'], 3000)

    def test_scenario_rejects_invalid_numbers_before_calculation(self):
        for parameter, value in [('lead_days', True), ('lead_days', 1.5),
                                 ('growth_pct', float('nan')), ('safety_days', float('inf'))]:
            with self.subTest(parameter=parameter, value=value), \
                    patch.object(agent, 'calculate_all') as calculate:
                result = agent.call_tool('simulate_scenario', {'parameter': parameter, 'value': value},
                                         self.dataset, self.settings, self.rows)
                self.assertIn('error', result)
                calculate.assert_not_called()

    def test_cached_values_are_isolated_from_mutating_consumers(self):
        cache = agent.ToolCache()
        compute = lambda: {'values': [{'qty': 25}]}
        first, hit = cache.run('example', {}, compute)
        self.assertFalse(hit)
        first['values'][0]['qty'] = 900
        second, hit = cache.run('example', {}, compute)
        self.assertTrue(hit)
        self.assertEqual(second['values'][0]['qty'], 25)
        second['values'][0]['qty'] = 500
        third, _ = cache.run('example', {}, compute)
        self.assertEqual(third['values'][0]['qty'], 25)

    def test_state_revision_invalidates_cache_and_new_dataset_is_isolated(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'ROOT', Path(folder)):
            state = app.State(demo=True)
            initial_cache, initial_revision = state.agent_cache, state.revision
            with patch.object(agent, 'call_tool', wraps=agent.call_tool) as call:
                for _ in range(2):
                    agent.ask('Сценарий роста 20%', state.dataset, state.settings, state.rows,
                              tool_cache=state.agent_cache)
                self.assertEqual(call.call_count, 1)
                state.settings['lead_days'] = 60
                state.recalculate()
                self.assertIsNot(state.agent_cache, initial_cache)
                self.assertNotEqual(state.revision, initial_revision)
                agent.ask('Сценарий роста 20%', state.dataset, state.settings, state.rows,
                          tool_cache=state.agent_cache)
                self.assertEqual(call.call_count, 2)
            other = app.State(mode='upload', dataset=make_demo())
            self.assertIsNot(other.agent_cache, state.agent_cache)
            self.assertNotEqual(other.revision, state.revision)


if __name__ == '__main__':
    unittest.main()
