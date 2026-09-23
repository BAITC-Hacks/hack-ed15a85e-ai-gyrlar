import copy
import unittest
from datetime import date

from replenishment.analytics import anomaly_report, backtest
from replenishment.engine import build_history, calculate, calculate_all, month_range


SETTINGS = dict(as_of='2026-09-22', lead_days=30, review_days=14, safety_days=7,
                trend=False, seasonality=True, remove_outliers=True, compensate_stockouts=True)


def product(code='001', unit='шт'):
    return dict(id='IEK:'+code, supplier='IEK', code=code, name='Тест '+code, unit=unit,
                category='A', sales={m: 100 for m in month_range(date(2026, 9, 1), 20)},
                stock=0, stock_date='2026-09-22', pack=1, moq=1, transit=[])


def dataset(*items):
    return dict(items=list(items), seasonality={'IEK': [1.0]*12}, as_of=SETTINGS['as_of'])


class AnomalyReviewTests(unittest.TestCase):
    def test_keep_restores_transaction_and_monthly_removed_demand(self):
        item = product()
        item['sales']['2026-08'] = 10000
        # The remaining 2000 still triggers the monthly guard.
        item['outlier_removed'] = {'2026-08': 8000}
        cleaned = calculate(item, [1]*12, SETTINGS)
        self.assertEqual(cleaned['history'][-1]['regular'], 100)
        item['anomaly_decisions'] = {'2026-08': 'keep'}
        kept = calculate(item, [1]*12, SETTINGS)
        month = kept['history'][-1]
        self.assertEqual(month['raw'], 10000)
        self.assertEqual(month['regular'], 10000)
        self.assertEqual(month['removed'], 0)
        self.assertEqual(month['candidate_regular'], 100)
        self.assertEqual(month['candidate_removed'], 9900)
        self.assertGreater(kept['qty'], cleaned['qty'])

    def test_explicit_exclude_works_with_global_cleaning_disabled(self):
        item = product()
        item['sales']['2026-08'] = 10000
        disabled = dict(SETTINGS, remove_outliers=False)
        automatic = calculate(item, [1]*12, disabled)
        item['anomaly_decisions'] = {'2026-08': 'exclude'}
        excluded = calculate(item, [1]*12, disabled)
        self.assertEqual(excluded['history'][-1]['regular'], 100)
        self.assertGreater(automatic['qty'], excluded['qty'])
        item['anomaly_decisions'] = {'2026-08': 'auto'}
        self.assertEqual(calculate(item, [1]*12, disabled)['qty'], automatic['qty'])

    def test_non_candidate_cannot_be_excluded_to_zero(self):
        item = product()
        item['anomaly_decisions'] = {'2026-08': 'exclude'}
        month = build_history(item, [1]*12, date(2026, 9, 22))[-1]
        self.assertEqual(month['regular'], 100)
        self.assertEqual(month['candidate_removed'], 0)

    def test_report_has_independent_candidates_and_isolated_order_impacts(self):
        item = product()
        item['sales']['2026-08'] = 10000
        item['anomaly_decisions'] = {'2026-08': 'keep'}
        data = dataset(item)
        original = copy.deepcopy(data)
        disabled = dict(SETTINGS, remove_outliers=False)
        report = anomaly_report(data, disabled, calculate_all(data, disabled))
        self.assertEqual(report['summary']['candidate_months'], 1)
        self.assertEqual(report['summary']['kept'], 1)
        month = report['items'][0]
        self.assertEqual(month['decision'], 'keep')
        self.assertEqual(month['regular'], 100)
        self.assertEqual(month['order_before'], month['current_order'])
        self.assertGreater(month['order_before'], month['order_after'])
        self.assertEqual(data, original)

    def test_report_pagination_preserves_complete_summary(self):
        items = [product(str(i)) for i in range(3)]
        for item in items:
            item['sales']['2026-08'] = 10000
        data = dataset(*items)
        rows = calculate_all(data, SETTINGS)
        first = anomaly_report(data, SETTINGS, rows, limit=2)
        last = anomaly_report(data, SETTINGS, rows, limit=2, offset=2)
        self.assertEqual(first['summary']['candidate_months'], 3)
        self.assertTrue(first['summary']['truncated'])
        self.assertEqual(len(first['items']), 2)
        self.assertFalse(last['summary']['truncated'])
        self.assertEqual(len(last['items']), 1)
        self.assertFalse({r['id'] for r in first['items']} & {r['id'] for r in last['items']})


class SupplierPolicyTests(unittest.TestCase):
    def test_supplier_terms_override_defaults_then_category_overrides_supplier(self):
        item = product()
        baseline = calculate(item, [1]*12, SETTINGS)
        settings = dict(SETTINGS, supplier_policies={'IEK': dict(lead_days=60, review_days=20, safety_days=14)})
        supplier = calculate(item, [1]*12, settings)
        self.assertEqual((supplier['lead_days'], supplier['review_days'], supplier['safety_days']), (60, 20, 14))
        self.assertEqual(supplier['horizon_days'], 80)
        self.assertGreater(supplier['qty'], baseline['qty'])
        settings['category_policies'] = {'A': dict(lead_days=10, safety_days=2)}
        category = calculate(item, [1]*12, settings)
        self.assertEqual((category['lead_days'], category['review_days'], category['safety_days']), (10, 20, 2))
        item['supplier'] = 'Other'
        settings.pop('category_policies')
        self.assertEqual(calculate(item, [1]*12, settings)['qty'], baseline['qty'])


class BacktestTests(unittest.TestCase):
    def test_stable_demand_is_exact_and_units_are_not_combined(self):
        result = backtest(dataset(product(), product('002', 'м')), SETTINGS)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['samples'], 6)
        self.assertEqual(result['products'], 2)
        self.assertEqual({m['unit'] for m in result['per_unit']}, {'шт', 'м'})
        for metrics in result['per_unit']:
            self.assertEqual(metrics['samples'], 3)
            self.assertEqual(metrics['actual_total'], 300)
            self.assertEqual(metrics['model']['mae'], 0)
            self.assertEqual(metrics['model']['wape'], 0)
            self.assertEqual(metrics['baseline']['mae'], 0)

    def test_heldout_month_and_future_do_not_change_its_prediction(self):
        item = product()
        initial = backtest(dataset(item), SETTINGS)
        item['sales'].update({'2026-06': 10000, '2026-07': 50000, '2026-08': 20000, '2026-09': 99999999})
        changed = backtest(dataset(item), SETTINGS)
        before = next(m for m in initial['by_month'] if m['month'] == '2026-06')
        after = next(m for m in changed['by_month'] if m['month'] == '2026-06')
        self.assertEqual(before['model']['predicted_total'], after['model']['predicted_total'])
        self.assertEqual(before['baseline']['predicted_total'], after['baseline']['predicted_total'])
        self.assertEqual(after['actual_total'], 10000)
        self.assertGreater(after['model']['mae'], 0)

    def test_full_history_metadata_and_business_scenarios_cannot_leak(self):
        item = product()
        item['sales']['2026-04'] = 8000
        data = dataset(item)
        initial = backtest(data, SETTINGS)
        item.update(stock=999999, transit=[dict(qty=999999, eta='2026-05-01')],
                    outlier_removed={m: qty for m, qty in item['sales'].items()},
                    stockout_days={m: 30 for m in item['sales']},
                    anomaly_decisions={m: 'keep' for m in item['sales']})
        data['seasonality']['IEK'] = [0.01]*11+[1000]
        altered = dict(SETTINGS, growth_pct=250, supplier_policies={'IEK': dict(lead_days=180)},
                       category_policies={'A': dict(growth_pct=100)})
        self.assertEqual(backtest(data, altered), initial)

    def test_short_or_gapped_history_is_unavailable(self):
        item = product()
        item['sales'] = {m: 100 for m in month_range(date(2026, 9, 1), 5)}
        result = backtest(dataset(item), SETTINGS)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['samples'], 0)
        self.assertEqual(result['skipped']['insufficient_history'], 3)
        item = product()
        item['sales'].pop('2026-05')
        self.assertEqual(backtest(dataset(item), SETTINGS)['status'], 'unavailable')

    def test_zero_actual_has_undefined_wape_not_a_fake_perfect_score(self):
        item = product()
        for month in ('2026-06', '2026-07', '2026-08'):
            item['sales'][month] = 0
        metrics = backtest(dataset(item), SETTINGS)['per_unit'][0]
        self.assertIsNone(metrics['model']['wape'])
        self.assertIsNone(metrics['baseline']['wape'])
        self.assertIsNone(metrics['wape_improvement_pp'])
        self.assertGreater(metrics['model']['mae'], 0)

    def test_missing_actual_not_silently_converted_to_zero(self):
        item = product()
        item['sales'].pop('2026-08')
        result = backtest(dataset(item), SETTINGS)
        self.assertEqual(result['samples'], 2)
        self.assertEqual(result['skipped']['missing_actual'], 1)


if __name__ == '__main__':
    unittest.main()
