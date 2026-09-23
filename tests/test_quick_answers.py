import copy
import unittest

from replenishment.agent import call_tool
from replenishment.demo import make_demo
from replenishment.engine import calculate_all
from replenishment.quick_answers import quick_answer


class QuickAnswerTests(unittest.TestCase):
    def setUp(self):
        self.data = make_demo()
        self.settings = dict(as_of='2026-09-22', lead_days=30, review_days=14,
                             safety_days=7, growth_pct=0, remove_outliers=True)
        self.rows = calculate_all(self.data, self.settings)
        self.calls = []

    def run_tool(self, name, args):
        self.calls.append((name, args))
        return call_tool(name, args, self.data, self.settings, self.rows)

    def answer(self, message, context=None):
        return quick_answer(message, self.data, self.settings, self.rows, self.run_tool, context)

    def test_known_questions_are_calculated_without_network(self):
        for question in ('Какие позиции требуют срочного заказа?', 'Что заказать в первую очередь?',
                         'Покажи разовые всплески продаж', 'Где обнаружены всплески?',
                         'Сценарий роста спроса 20%', 'Что изменится при росте спроса на 20%?'):
            with self.subTest(question=question):
                result = self.answer(question)
                self.assertIsInstance(result['answer'], str)
                self.assertIn('Следующий шаг:', result['answer'])
                self.assertLess(len(result['answer']), 1800)

    def test_free_form_and_detailed_requests_fall_through(self):
        for question in ('Какие позиции требуют срочного заказа? Объясни подробно',
                         'Расскажи про кабели', 'Что заказать для IEK?',
                         'Объясни расчёт для DEMO-001 подробно',
                         'Сценарий роста спроса 20% и срока поставки 45 дней',
                         'Объясни расчёт для отсутствующего товара'):
            with self.subTest(question=question):
                self.assertIsNone(self.answer(question))
        self.assertEqual(self.calls, [])

    def test_draft_reservations_and_early_shortage_are_not_duplicate_orders(self):
        row = self.rows[0]
        row.update(qty=100, draft_qty=100, available_to_order=0, urgency='Срочно')
        self.rows = [row]
        text = self.answer('Что заказать в первую очередь?')['answer']
        self.assertIn('уже в черновиках: 100', text)
        self.assertIn('повторный заказ не нужен', text)
        self.assertIn('новый заказ не закрывает дефицит до прибытия', text)
        row.update(qty=0, draft_qty=0, available_to_order=0)
        text = self.answer('Что заказать в первую очередь?')['answer']
        self.assertIn('дополнительный заказ не нужен; ускорьте поступление', text)

    def test_unknown_stock_is_not_interpreted_as_zero(self):
        row = self.rows[0]
        row.update(stock=None, qty=None, available_to_order=0, status='Нужны данные',
                   urgency='Нет остатка', warnings=['Нет числового остатка. Количество заказа не рассчитано'])
        self.rows = [row]
        text = self.answer('Объясни расчёт для '+row['article'])['answer']
        self.assertIn('Остаток неизвестен', text)
        self.assertNotIn('черновик: 0', text)
        self.assertIn('1 позиций остаток неизвестен', self.answer('Что заказать в первую очередь?')['answer'])

    def test_partial_draft_and_manager_quantity_follow_creation_rules(self):
        row = self.rows[0]
        row.update(qty=20, pack=10, moq=0, draft_qty=15, available_to_order=5)
        self.rows = [row]
        prompt = 'Объясни расчёт для '+row['article']
        self.assertIn('новый черновик: 10', self.answer(prompt)['answer'])
        row['review'] = {'qty': 0}
        self.assertIn('новый черновик: 0', self.answer(prompt)['answer'])
        self.assertIn('Учтена правка менеджера: 0', self.answer(prompt)['answer'])
        row.update(review={'qty': 50}, moq=40)
        self.assertIn('новый черновик: 40', self.answer(prompt)['answer'])

    def test_scenario_preserves_unknown_and_preliminary_warnings(self):
        self.rows[0].update(stock=None, qty=None)
        self.rows[1]['status'] = 'Предварительно'
        text = self.answer('Сценарий роста спроса 20%')['answer']
        self.assertIn('остаток неизвестен: количество не рассчитано', text)
        self.assertIn('расчёт предварительный', text)

    def test_stale_date_and_shortage_warning_remain_visible(self):
        row = self.rows[0]
        row.update(stock_date='2026-08-01', status='Предварительно',
                   warnings=['Остаток на 2026-08-01; расчёт предварительный',
                             'Дефицит до новой поставки: требуется ускорение или перемещение'])
        self.rows = [row]
        text = self.answer('Объясни расчёт для '+row['article'])['answer']
        self.assertIn('2026-08-01', text)
        self.assertIn('Дефицит до новой поставки', text)

    def test_ambiguous_article_requires_exact_context(self):
        first = self.rows[0]
        second = dict(first, id='OTHER:1', supplier='Другой поставщик')
        self.rows = [first, second]
        prompt = 'Объясни расчёт для '+first['article']
        self.assertIn('соответствует 2 товарам', self.answer(prompt)['answer'])
        self.assertEqual(self.calls, [])
        self.assertIn('Другой поставщик', self.answer(prompt, {'item_id': 'OTHER:1'})['answer'])
        self.assertEqual(self.calls[-1], ('explain_product', {'id': 'OTHER:1'}))
        self.assertIn('соответствует 2 товарам', self.answer(prompt, {'item_id': 'INVALID'})['answer'])

    def test_scenario_is_read_only_and_delta_preserves_units(self):
        before = copy.deepcopy((self.settings, self.rows, self.data))
        text = self.answer('Сценарий роста спроса 20,5%')['answer']
        self.assertIn('+20.5%', text)
        self.assertIn('Параметры не сохранены', text)
        self.assertEqual((self.settings, self.rows, self.data), before)
        self.assertEqual(self.calls[-1], ('simulate_scenario', {'parameter': 'growth_pct', 'value': 20.5}))
        self.assertTrue('шт' in text or 'м ' in text)

    def test_scenario_bounds_prevent_invalid_calculation(self):
        for value in ('301', '-91', '9'*350):
            self.assertIn('от −90% до 300%', self.answer('Сценарий роста спроса '+value+'%')['answer'])
        self.assertEqual(self.calls, [])

    def test_kept_anomaly_is_a_candidate_not_claimed_removed(self):
        row = self.rows[0]
        row.update(removed=0, history=[{'candidate_removed': 100, 'removed': 0, 'decision': 'keep'}])
        self.rows = [row]
        text = self.answer('Где обнаружены всплески?')['answer']
        self.assertIn('продажи сохранены в расчёте', text)
        self.assertIn('не подтверждённые проекты', text)


if __name__ == '__main__':
    unittest.main()
