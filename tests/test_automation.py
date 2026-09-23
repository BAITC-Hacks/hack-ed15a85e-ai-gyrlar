import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from replenishment.automation import collect_sources, validate_config
from replenishment.uploads import build_dataset, merge_source, template_bytes


class AutomationTests(unittest.TestCase):
    def test_config_defaults_partial_update_and_input_preservation(self):
        current = dict(enabled=False, interval_minutes=30, advance_date=False, folder='', last_run='2026-09-23T10:00:00')
        request = dict(enabled=True)
        before = copy.deepcopy(current), copy.deepcopy(request)
        result = validate_config(request, current)
        self.assertEqual(result, dict(enabled=True, interval_minutes=30, advance_date=False, folder=''))
        self.assertEqual((current, request), before)
        self.assertEqual(validate_config({})['interval_minutes'], 60)

    def test_config_validates_boolean_and_integer_types_and_limits(self):
        for changes in [dict(enabled=1), dict(advance_date='false'), dict(interval_minutes=True),
                        dict(interval_minutes='30'), dict(interval_minutes=1.5),
                        dict(interval_minutes=0), dict(interval_minutes=10081), dict(folder=12),
                        dict(unknown=True)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_config(changes)
        self.assertEqual(validate_config({'interval_minutes': 1})['interval_minutes'], 1)
        self.assertEqual(validate_config({'interval_minutes': 10080})['interval_minutes'], 10080)

    def test_config_folder_absolute_existing_when_enabled(self):
        with tempfile.TemporaryDirectory() as folder:
            result = validate_config(dict(enabled=True, folder=folder))
            self.assertEqual(Path(result['folder']), Path(folder).resolve())
            missing = str(Path(folder)/'new')
            self.assertEqual(validate_config(dict(enabled=False, folder=missing))['folder'], missing)
            with self.assertRaisesRegex(ValueError, 'не найдена'):
                validate_config(dict(enabled=True, folder=missing))
            regular = Path(folder)/'file.txt'
            regular.write_text('data', encoding='utf-8')
            with self.assertRaises(ValueError):
                validate_config(dict(enabled=True, folder=str(regular)))
        with self.assertRaisesRegex(ValueError, 'абсолютный'):
            validate_config(dict(folder='relative'))
        self.assertEqual(validate_config(dict(enabled=True, folder='  '))['folder'], '')

    def test_blank_folder_means_recalculate_only(self):
        with patch.object(Path, 'iterdir', side_effect=AssertionError('No file I/O expected')):
            self.assertEqual(collect_sources('', {'sales': 'old'}), ({}, {}))

    def test_three_sources_collect_and_apply_in_order(self):
        with tempfile.TemporaryDirectory() as folder:
            for kind in ('sales', 'stock', 'transit'):
                (Path(folder)/(kind+'.csv')).write_bytes(template_bytes(kind))
            (Path(folder)/'notes.txt').write_text('ignore me', encoding='utf-8')
            changed, hashes = collect_sources(folder)
            self.assertEqual(list(changed), ['sales', 'stock', 'transit'])
            self.assertEqual(set(hashes), set(changed))
            sales = changed['sales']
            dataset = build_dataset(sales['upload'], sales['options'])
            for kind in ('stock', 'transit'):
                source = changed[kind]
                dataset = merge_source(dataset, source['upload'], source['options'], kind)
                self.assertEqual(source['hash'], hashes[kind])
            self.assertEqual(set(dataset['source_imports']), {'sales', 'stock', 'transit'})
            self.assertTrue(all(item['stock'] is not None for item in dataset['items']))

    def test_hashes_detect_only_changes_and_do_not_mutate_caller(self):
        with tempfile.TemporaryDirectory() as folder:
            sales = Path(folder)/'sales.csv'
            sales.write_bytes(template_bytes('sales'))
            _, hashes = collect_sources(folder)
            before = copy.deepcopy(hashes)
            unchanged, same_hashes = collect_sources(folder, hashes)
            self.assertEqual(unchanged, {})
            self.assertEqual(same_hashes, hashes)
            self.assertIsNot(same_hashes, hashes)
            sales.write_bytes(sales.read_bytes()+b'\r\n')
            changed, next_hashes = collect_sources(folder, hashes)
            self.assertEqual(list(changed), ['sales'])
            self.assertNotEqual(next_hashes['sales'], hashes['sales'])
            self.assertEqual(hashes, before)
            sales.unlink()
            self.assertEqual(collect_sources(folder, hashes), ({}, {}))

    def test_duplicate_kind_files_and_bad_other_file_aggregate_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'sales.csv').write_bytes(template_bytes('sales'))
            (Path(folder)/'sales.xlsx').write_bytes(b'not an excel file')
            (Path(folder)/'stock.csv').write_text('broken', encoding='utf-8')
            hashes = {'sales': 'old'}
            with self.assertRaises(ValueError) as exc:
                collect_sources(folder, hashes)
            self.assertIn('несколько файлов', str(exc.exception))
            self.assertIn('stock.csv', str(exc.exception))
            self.assertEqual(hashes, {'sales': 'old'})

    def test_case_insensitive_names_and_requested_date_supplier(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'STOCK.CSV').write_text('Код;Остаток\n001;4', encoding='utf-8-sig')
            changed, _ = collect_sources(folder, as_of='2026-09-23', supplier='IEK')
            self.assertEqual(changed['stock']['options']['as_of'], '2026-09-23')
            self.assertEqual(changed['stock']['options']['supplier'], 'IEK')

    def test_missing_automatic_mapping_rejects_named_file(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'transit.csv').write_text('Код;В пути\n001;4', encoding='utf-8-sig')
            with self.assertRaisesRegex(ValueError, 'transit.csv.*Дата поступления'):
                collect_sources(folder)

    def test_bounded_reads_and_non_file_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            sales = Path(folder)/'sales.csv'
            sales.write_bytes(template_bytes('sales'))
            with patch('replenishment.automation.MAX_BYTES', 12), self.assertRaisesRegex(ValueError, 'больше 20 МБ'):
                collect_sources(folder)
            sales.unlink()
            sales.mkdir()
            with self.assertRaisesRegex(ValueError, 'ожидался файл'):
                collect_sources(folder)

    def test_excel_requires_one_nonempty_sheet(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as folder:
            book = Workbook()
            for sheet in [book.active, book.create_sheet('Second')]:
                sheet.append(['Код', 'Остаток'])
                sheet.append(['001', 4])
            book.save(Path(folder)/'stock.xlsx')
            with self.assertRaisesRegex(ValueError, 'один непустой лист'):
                collect_sources(folder)
            book.remove(book['Second'])
            book.save(Path(folder)/'stock.xlsx')
            changed, _ = collect_sources(folder)
            self.assertEqual(changed['stock']['options']['sheet'], 0)

    def test_monthly_sales_require_only_code_and_month_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'sales.csv').write_text('Код;2026-01;2026-02\n001;5;6', encoding='utf-8-sig')
            changed, _ = collect_sources(folder, as_of='2026-09-23')
            self.assertEqual(changed['sales']['options']['mode'], 'monthly')
            self.assertEqual(build_dataset(changed['sales']['upload'], changed['sales']['options'])['items'][0]['sales']['2026-02'], 6)


if __name__ == '__main__':
    unittest.main()
