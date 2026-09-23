import copy
import unittest
from datetime import date

from replenishment.engine import calculate_all
from replenishment.uploads import build_dataset, inspect_file, merge_source, preview, template_bytes


class MultiSourceTests(unittest.TestCase):
    def parse(self, text, filename='source.csv'):
        return inspect_file(text.encode('utf-8-sig'), filename)

    def options(self, upload, kind='sales', **changes):
        sheet = preview(upload, kind)['sheets'][0]
        result = dict(sheet=0, mode=sheet['mode'], mapping=sheet['mapping'], as_of='2026-09-23')
        result.update(changes)
        return result

    def dataset(self):
        upload = self.parse('Код;Поставщик;2026-01;2026-02;2026-03;2026-04;2026-05;2026-06;2026-07;2026-08;Остаток;Кратность;Единица\n'
                            '001;IEK;20;20;20;20;20;20;20;20;4;1;шт\n'
                            '001;Systeme Electric;10;10;10;10;10;10;10;10;5;1;шт\n'
                            '002;IEK;30;30;30;30;30;30;30;30;7;1;шт', 'sales.csv')
        dataset = build_dataset(upload, self.options(upload))
        # Original partner IDs do not have the same format as the upload IDs.
        for item in dataset['items']:
            item['id'] = item['supplier']+':'+item['code']
        return dataset

    def merge(self, dataset, text, kind='stock', **changes):
        upload = self.parse(text, kind+'.csv')
        return merge_source(dataset, upload, self.options(upload, kind, **changes), kind)

    def test_authoritative_stock_snapshot_preserves_ids_history_and_input(self):
        dataset = self.dataset()
        before = copy.deepcopy(dataset)
        result = self.merge(dataset, 'Код;Поставщик;Остаток;Дата остатка;Кратность;Минимальная партия\n001;IEK;12;2026-09-22;6;12\n002;IEK;0;2026-09-23;1;0')
        self.assertEqual(dataset, before)
        self.assertEqual(result['items'][0]['id'], 'IEK:001')
        self.assertEqual(result['items'][0]['sales'], before['items'][0]['sales'])
        self.assertEqual(result['items'][0]['stock'], 12)
        self.assertEqual(result['items'][0]['pack'], 6)
        self.assertEqual(result['items'][0]['moq'], 12)
        self.assertIsNone(result['items'][1]['stock'])
        self.assertIsNone(result['items'][1]['stock_date'])
        self.assertEqual(result['items'][2]['stock'], 0)
        self.assertIn('неизвестен', ' '.join(result['items'][1]['warnings']))
        rows = calculate_all(result, {'as_of': '2026-09-23'})
        self.assertIsNone(next(row for row in rows if row['id'] == 'Systeme Electric:001')['qty'])
        self.assertIsNotNone(next(row for row in rows if row['id'] == 'IEK:002')['qty'])

    def test_supplier_is_required_only_for_ambiguous_codes(self):
        dataset = self.dataset()
        with self.assertRaisesRegex(ValueError, 'нескольких поставщиков'):
            self.merge(dataset, 'Код;Остаток\n001;12')
        result = self.merge(dataset, 'Код;Остаток\n002;12')
        self.assertEqual(result['items'][2]['stock'], 12)
        fallback = self.merge(dataset, 'Код;Остаток\n001;12', supplier='IEK')
        self.assertEqual(fallback['items'][0]['stock'], 12)
        self.assertIsNone(fallback['items'][1]['stock'])

    def test_unknown_product_rejected_atomically_after_valid_rows(self):
        dataset = self.dataset()
        before = copy.deepcopy(dataset)
        with self.assertRaisesRegex(ValueError, 'Строка 3.*Сначала загрузите продажи'):
            self.merge(dataset, 'Код;Поставщик;Остаток\n002;IEK;8\nNEW;IEK;9')
        self.assertEqual(dataset, before)
        with self.assertRaisesRegex(ValueError, 'не найден'):
            self.merge(dataset, 'Код;Поставщик;Остаток\n002;Other;8')

    def test_duplicate_equal_stock_is_deduplicated_not_summed(self):
        dataset = self.dataset()
        result = self.merge(dataset, 'Код;Остаток\n002;8\n002;8')
        self.assertEqual(result['items'][2]['stock'], 8)
        self.assertEqual(result['source_imports']['stock']['diagnostics']['duplicate_equal_stock_rows'], 1)
        with self.assertRaisesRegex(ValueError, 'разные остатки'):
            self.merge(dataset, 'Код;Остаток\n002;8\n002;9')

    def test_reimport_stock_replaces_source_and_has_no_duplicate_warnings(self):
        dataset = self.dataset()
        content = 'Код;Остаток\n002;8'
        once = self.merge(dataset, content)
        twice = self.merge(once, content)
        self.assertEqual(once['items'], twice['items'])
        self.assertEqual(len(once['sources']), len(twice['sources']))
        self.assertEqual(once['limitations'], twice['limitations'])
        self.assertEqual(twice['source_imports']['stock']['row_count'], 1)
        self.assertEqual(twice['source_imports']['stock']['as_of'], '2026-09-23')

    def test_missing_stock_date_explicit_warning_and_date_validation(self):
        dataset = self.dataset()
        result = self.merge(dataset, 'Код;Остаток\n002;8')
        self.assertEqual(result['items'][2]['stock_date'], '2026-09-23')
        self.assertIn('Дата остатка не указана', ' '.join(result['items'][2]['warnings']))
        self.assertEqual(result['source_imports']['stock']['diagnostics']['assumed_stock_dates'], 1)
        for value in ['bad', '2026-09-24']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.merge(dataset, f'Код;Остаток;Дата остатка\n002;8;{value}')

    def test_transit_multiple_eta_orders_and_repeat_replace(self):
        dataset = self.dataset()
        dataset['items'][0]['transit'] = [dict(qty=100, eta='2026-10-01')]
        content = ('Код;Количество;Дата поступления;Номер заказа поставщику\n'
                   '002;4;2026-09-25;PO-1\n002;6;2026-10-04;PO-2\n002;2;2026-10-04;PO-3')
        once = self.merge(dataset, content, 'transit')
        twice = self.merge(once, content, 'transit')
        self.assertEqual(once['items'], twice['items'])
        self.assertEqual(once['items'][0]['transit'], [])
        shipments = once['items'][2]['transit']
        self.assertEqual([row['order_id'] for row in shipments], ['PO-1', 'PO-2', 'PO-3'])
        self.assertEqual(sum(row['qty'] for row in shipments), 12)
        rows = calculate_all(once, {'as_of': '2026-09-23'})
        self.assertEqual(next(row for row in rows if row['id'] == 'IEK:002')['in_transit'], 12)
        replaced = self.merge(twice, 'Код;В пути;Дата поступления\n002;3;2026-09-30', 'transit')
        self.assertEqual(len(replaced['items'][2]['transit']), 1)
        self.assertEqual(replaced['items'][2]['transit'][0]['qty'], 3)

    def test_transit_duplicate_identity_is_rejected_without_mutation(self):
        dataset = self.dataset()
        before = copy.deepcopy(dataset)
        for order in ['', ';PO-1']:
            title = 'Код;В пути;Дата поступления'+(';Номер заказа' if order else '')
            row = '002;4;2026-10-01'+order
            with self.subTest(order=order), self.assertRaisesRegex(ValueError, 'повтор поставки'):
                self.merge(dataset, title+'\n'+row+'\n'+row, 'transit')
        self.assertEqual(dataset, before)

    def test_transit_requires_eta_finite_nonnegative_quantity_and_consistent_unit(self):
        dataset = self.dataset()
        for row in ['002;4;', '002;4;bad', '002;-1;2026-10-01', '002;NaN;2026-10-01']:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.merge(dataset, 'Код;В пути;Дата поступления\n'+row, 'transit')
        with self.assertRaisesRegex(ValueError, 'единица'):
            self.merge(dataset, 'Код;В пути;Дата поступления;Единица\n002;4;2026-10-01;упаковка', 'transit')

    def test_overdue_transit_preserved_but_excluded_by_calculator(self):
        result = self.merge(self.dataset(), 'Код;В пути;Дата поступления\n002;100;2026-09-01\n002;5;2026-09-29', 'transit')
        row = next(row for row in calculate_all(result, {'as_of': '2026-09-23'}) if row['id'] == 'IEK:002')
        self.assertEqual(row['total_transit'], 105)
        self.assertEqual(row['in_transit'], 5)
        self.assertEqual(result['source_imports']['transit']['diagnostics']['overdue_shipments'], 1)

    def test_zero_transit_snapshot_clears_all_shipments(self):
        dataset = self.dataset()
        dataset['items'][0]['transit'] = [dict(qty=10, eta='2026-09-25')]
        result = self.merge(dataset, 'Код;В пути;Дата поступления\n002;0;2026-09-23', 'transit')
        self.assertTrue(all(not item['transit'] for item in result['items']))

    def test_source_preview_remaps_generic_quantity_and_date_without_mutation(self):
        upload = self.parse('Код;Количество;Дата\n002;12;2026-10-01')
        before = copy.deepcopy(upload)
        result = preview(upload, 'transit')
        self.assertEqual(result['sheets'][0]['mapping']['transit'], 1)
        self.assertEqual(result['sheets'][0]['mapping']['eta'], 2)
        self.assertEqual(result['required'], ['code', 'transit', 'eta'])
        self.assertNotIn('qty', result['sheets'][0]['mapping'])
        self.assertEqual(upload, before)
        self.assertTrue(result['warnings'])

    def test_generic_quantity_mapping_accepted_by_merge(self):
        upload = self.parse('Код;Количество;Дата поступления\n002;12;2026-10-01')
        options = dict(sheet=0, mapping=upload['tables'][0]['mapping'], as_of='2026-09-23')
        result = merge_source(self.dataset(), upload, options, 'transit')
        self.assertEqual(result['items'][2]['transit'][0]['qty'], 12)

    def test_templates_work_together_and_keep_provenance(self):
        sales = inspect_file(template_bytes('sales'), 'sales.csv')
        options = self.options(sales, as_of=date.today().isoformat())
        dataset = build_dataset(sales, options)
        for kind in ('stock', 'transit'):
            upload = inspect_file(template_bytes(kind), kind+'.csv')
            dataset = merge_source(dataset, upload, self.options(upload, kind, as_of=date.today().isoformat()), kind)
        self.assertEqual(set(dataset['source_imports']), {'sales', 'stock', 'transit'})
        self.assertEqual(len(dataset['sources']), 3)
        for kind, source in dataset['source_imports'].items():
            self.assertEqual(source['type'], kind)
            self.assertEqual(len(source['hash']), 64)
            self.assertGreater(source['row_count'], 0)
            self.assertIn('+00:00', source['imported_at'])
        results = calculate_all(dataset, {'as_of': date.today().isoformat()})
        self.assertEqual(len(results), 2)
        self.assertTrue(all(row['qty'] is not None for row in results))
        self.assertEqual(next(row for row in results if row['code'] == 'A-001')['in_transit'], 18)

    def test_merge_requires_sales_and_valid_mapping(self):
        upload = self.parse('Код;Остаток\n002;1')
        with self.assertRaisesRegex(ValueError, 'Сначала загрузите историю'):
            merge_source({'items': []}, upload, self.options(upload, 'stock'), 'stock')
        with self.assertRaisesRegex(ValueError, 'нескольким полям'):
            merge_source(self.dataset(), upload, self.options(upload, 'stock', mapping={'code': 0, 'stock': 0}), 'stock')


if __name__ == '__main__':
    unittest.main()
