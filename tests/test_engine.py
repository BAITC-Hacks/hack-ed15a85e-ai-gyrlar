import copy
import csv
import io
import json
import math
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from replenishment.engine import calculate, clean_transactions, build_history
from replenishment.importer import apply_stockouts
from replenishment.agent import call_tool, ask_live
from replenishment.demo import make_demo
from app import DEFAULTS, validate_settings, csv_bytes, State


def product():
    return dict(id='IEK:001', supplier='IEK', code='001', article='TEST', name='Тест', unit='шт',
                category='A', stock=0, stock_date='2026-09-22', pack=1, moq=1,
                sales={f'{y}-{m:02}': 120 for y in (2024, 2025, 2026) for m in range(1, 13) if (y, m) <= (2026, 8)},
                transit=[], sources=[], warnings=[])


class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.item = product()
        self.settings = dict(DEFAULTS, trend=False)
    def calc(self, item=None, **settings):
        return calculate(item or self.item, [1]*12, dict(self.settings, **settings))

    def test_inventory_and_in_transit_reduce_order(self):
        original = self.calc()['qty']
        self.item['stock'] = 20
        self.assertEqual(self.calc()['qty'], original-20)
        self.item['transit'] = [dict(qty=30, eta='2026-10-01')]
        self.assertEqual(self.calc()['qty'], original-50)

    def test_receipt_outside_horizon_and_overdue_not_counted(self):
        original = self.calc()['qty']
        self.item['transit'] = [dict(qty=10000, eta='2027-01-01'), dict(qty=10000, eta='2026-09-20')]
        self.assertEqual(self.calc()['qty'], original)

    def test_late_receipt_does_not_hide_early_shortage(self):
        self.item['transit'] = [dict(qty=10000, eta='2026-10-15')]
        result = self.calc()
        self.assertEqual(result['qty'], 0)
        self.assertEqual(result['urgency'], 'Срочно')
        self.assertEqual(result['shortage_days'], 0)

    def test_missing_stock_is_not_zero(self):
        self.item['stock'] = None
        self.assertIsNone(self.calc()['qty'])
        self.assertEqual(self.calc()['status'], 'Нужны данные')

    def test_stale_snapshot_is_preliminary(self):
        self.item['stock_date'] = '2026-09-01'
        self.assertEqual(self.calc()['status'], 'Предварительно')

    def test_zero_demand_does_not_trigger_moq(self):
        self.item['sales'] = {m: 0 for m in self.item['sales']}
        self.item['moq'] = 1000
        self.assertEqual(self.calc()['qty'], 0)

    def test_pack_and_minimum_are_distinct(self):
        self.item['stock'] = 195
        self.item.update(moq=100, pack=30)
        self.assertEqual(self.calc()['qty'], 120)
        self.item['stock'] = 10000
        self.assertEqual(self.calc()['qty'], 0)

    def test_explicit_stockout_increases_demand(self):
        baseline = self.calc()['demand']
        self.item['stockout_days'] = {'2026-08': 16, '2026-07': 15}
        corrected = self.calc()
        self.assertGreater(corrected['demand'], baseline)
        self.assertGreater(corrected['lost'], 0)
        self.assertAlmostEqual(self.calc(compensate_stockouts=False)['demand'], baseline)

    def test_full_stockout_is_imputed(self):
        self.item['sales']['2026-08'] = 0
        no_correction = self.calc()['demand']
        self.item['stockout_days'] = {'2026-08': 31}
        self.assertGreater(self.calc()['demand'], no_correction)

    def test_snapshot_zero_does_not_invent_stockout(self):
        self.item['stock_history'] = {'2026-08': 0}
        self.assertEqual(self.calc()['lost'], 0)

    def test_partial_month_is_excluded(self):
        baseline = self.calc()['qty']
        self.item['sales']['2026-09'] = 10000000
        self.assertEqual(self.calc()['qty'], baseline)

    def test_growth_and_category_policy_change_result(self):
        baseline = self.calc()['qty']
        self.assertGreater(self.calc(growth_pct=25)['qty'], baseline)
        self.assertGreater(self.calc(category_policies={'A': {'safety_days': 30}})['qty'], baseline)

    def test_seasonal_forecast_follows_season(self):
        season = [1]*12
        season[9] = 2
        seasonal = calculate(self.item, season, self.settings)
        flat = self.calc()
        self.assertGreater(seasonal['demand'], flat['demand'])

    def test_sustained_trend_is_detected(self):
        for m in ('2026-06', '2026-07', '2026-08'):
            self.item['sales'][m] = 180
        self.assertGreater(self.calc(trend=True)['growth'], 0)
        self.assertEqual(self.calc(trend=False)['growth'], 0)

    def test_isolated_monthly_spike_has_limited_effect(self):
        baseline = self.calc()['qty']
        self.item['sales']['2026-08'] = 12000
        self.assertAlmostEqual(self.calc()['qty'], baseline, delta=2)
        self.assertGreater(self.calc(remove_outliers=False)['qty'], baseline*3)

    def test_outlier_cleaning_ignores_return(self):
        rows = [dict(date=f'2026-08-{i+1:02}', order_id=str(i), qty=10) for i in range(20)]
        rows += [dict(date='2026-08-28', order_id='spike', qty=10000), dict(date='2026-08-29', order_id='return', qty=-10000)]
        cleaned = clean_transactions(rows)
        self.assertEqual(sum(r['clean'] for r in cleaned), 210)

    def test_split_documents_same_customer_are_detected(self):
        rows = [dict(date=f'2026-08-{i+1:02}', order_id=str(i), customer_id=f'anon-{i}', qty=10) for i in range(20)]
        rows += [dict(date='2026-08-28', order_id='split'+str(i), customer_id='anon-big', qty=10) for i in range(20)]
        cleaned = clean_transactions(rows)
        big = [r for r in cleaned if r['customer_id'] == 'anon-big']
        self.assertEqual(sum(r['qty'] for r in big), 200)
        self.assertEqual(sum(r['clean'] for r in big), 10)

    def test_split_lines_of_one_order_are_aggregated(self):
        rows = [dict(date='2026-08-01', order_id=str(i), qty=10) for i in range(20)]
        rows += [dict(date='2026-08-02', order_id='big', qty=50) for i in range(20)]
        self.assertEqual(sum(r['clean'] for r in clean_transactions(rows)), 210)

    def test_regular_large_orders_are_preserved(self):
        rows = [dict(date=f'2026-08-{i+1:02}', order_id=str(i), qty=500) for i in range(20)]
        self.assertEqual(sum(r['clean'] for r in clean_transactions(rows)), 10000)

    def test_overlapping_stockouts_not_double_counted(self):
        dataset = {'items': [self.item]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'stockouts.csv'
            path.write_text('supplier,code,start,end\nIEK,001,2026-08-01,2026-08-10\nIEK,001,2026-08-05,2026-08-15\n', encoding='utf-8')
            apply_stockouts(dataset, path)
        self.assertEqual(self.item['stockout_days']['2026-08'], 15)


class WorkflowTests(unittest.TestCase):
    def test_invalid_inputs_rejected(self):
        for values in [dict(lead_days=0), dict(lead_days=1.5), dict(growth_pct=float('nan')), dict(trend='false')]:
            with self.assertRaises(ValueError):
                validate_settings(values)

    def test_zero_manual_quantity_is_preserved_and_invalidated_on_change(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.ROOT', Path(folder)):
            state = State(demo=True)
            row = state.rows[0]
            state.review(dict(id=row['id'], qty=0, confirmed=True, note='Отложен'))
            self.assertEqual(state.rows[0]['review']['qty'], 0)
            state.recalculate()
            self.assertEqual(next(r for r in state.rows if r['id'] == row['id'])['review']['qty'], 0)
            state.settings['lead_days'] = 45
            state.recalculate()
            self.assertFalse(state.reviews)

    def test_no_approval_without_confirmation_or_fresh_stock(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.ROOT', Path(folder)):
            state = State(demo=True)
            row = state.rows[0]
            with self.assertRaises(ValueError):
                state.review(dict(id=row['id'], qty=0, confirmed=False))
            state.overrides[row['id']] = dict(stock_date='2026-09-01')
            state.recalculate()
            with self.assertRaises(ValueError):
                state.review(dict(id=row['id'], qty=0, confirmed=True))

    def test_csv_preserves_ids_bom_and_prevents_formula_injection(self):
        row = calculate(product(), [1]*12, DEFAULTS)
        row['name'] = '=HYPERLINK("bad")'
        row['review'] = dict(qty=0)
        data = csv_bytes([row])
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        rows = list(csv.reader(io.StringIO(data.decode('utf-8-sig')), delimiter=';'))
        self.assertEqual(rows[1][1], '001')
        self.assertTrue(rows[1][3].startswith("'="))
        self.assertEqual(rows[1][6], '0')

    def test_scenario_does_not_mutate_active_settings(self):
        data = make_demo()
        settings = copy.deepcopy(DEFAULTS)
        from replenishment.engine import calculate_all
        result = call_tool('simulate_scenario', dict(parameter='growth_pct', value=25), data, settings, calculate_all(data, settings))
        self.assertGreater(result['changed_count'], 0)
        self.assertEqual(settings, DEFAULTS)
        self.assertFalse(result['applied'])

    def test_real_agent_tool_loop_with_mocked_transport(self):
        from replenishment.engine import calculate_all
        data = make_demo()
        responses = [dict(output=[dict(type='function_call', name='inventory_summary', arguments='{}', call_id='test', id='fc1')]),
                     dict(output=[dict(type='message', content=[dict(type='output_text', text='Результат основан на расчёте')])])]
        with patch('replenishment.agent.remote_request', side_effect=responses) as remote:
            result = ask_live('Покажи сводку', data, DEFAULTS, calculate_all(data, DEFAULTS))
            self.assertEqual(result['mode'], 'live')
            self.assertEqual(result['trace'][0]['tool'], 'inventory_summary')
            self.assertEqual(remote.call_count, 2)
            self.assertIn('function_call_output', str(remote.call_args))


if __name__ == '__main__':
    unittest.main()
