import copy
import io
import json
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

import app
from replenishment.engine import calculate_all
from replenishment.uploads import inspect_file, preview, build_dataset


class UploadTests(unittest.TestCase):
    def parse(self, text, filename='продажи.csv'):
        return inspect_file(text.encode('utf-8-sig'), filename)

    def options(self, upload, **changes):
        sheet = upload['tables'][0]
        result = dict(sheet=0, mode=sheet['mode'], mapping=sheet['mapping'], as_of='2026-09-23')
        result.update(changes)
        return result

    def test_long_sales_cleaning_returns_stock_and_zero_months(self):
        rows = ['Код;Поставщик;Дата;Количество;Остаток;Кратность;Единица']
        for m in range(1, 9):
            rows.append(f'001;Тест;2026-{m:02}-10;10;7;5;шт')
        rows += ['001;Тест;2026-08-11;200;7;5;шт', '001;Тест;2026-08-12;-2;7;5;шт',
                 '002;Тест;2026-01-10;5;;1;шт']
        upload = self.parse('\n'.join(rows))
        dataset = build_dataset(upload, self.options(upload))
        a, b = dataset['items']
        self.assertEqual(a['code'], '001')
        self.assertEqual(a['stock'], 7)  # A repeated snapshot is not summed.
        self.assertEqual(a['sales']['2026-08'], 208)
        self.assertEqual(a['outlier_removed']['2026-08'], 190)
        self.assertEqual(b['sales']['2026-08'], 0)
        result = calculate_all(dataset, dict(app.DEFAULTS, as_of='2026-09-23'))
        self.assertIsNone(next(r for r in result if r['code'] == '002')['qty'])
        self.assertGreater(next(r for r in result if r['code'] == '001')['qty'], 0)
        from replenishment.agent import call_tool, TOOLS
        found = call_tool('find_products', {'query': '001', 'supplier': 'Тест'}, dataset, app.DEFAULTS, result)
        self.assertEqual(found['total'], 1)
        self.assertEqual(found['products'][0]['stock'], 7)
        self.assertNotIn('enum', next(t for t in TOOLS if t['name'] == 'find_products')['parameters']['properties']['supplier'])

    def test_monthly_partner_headers_and_decimal_comma(self):
        upload = self.parse('Номенклатура.Код;Номенклатура;Январь 2026;2026-02;03.2026;2026-09;Остаток\n001;Лампа;12,5;;3;999;0')
        dataset = build_dataset(upload, self.options(upload))
        self.assertEqual(dataset['items'][0]['sales']['2026-01'], 12.5)
        self.assertEqual(dataset['items'][0]['sales']['2026-02'], 0)
        self.assertEqual(dataset['upload']['period'], ['2026-01', '2026-03'])
        history = calculate_all(dataset, dict(app.DEFAULTS, as_of='2026-09-23'))[0]['history']
        self.assertFalse(any(r['month'] == '2026-09' for r in history))

    def test_excel_dates_sheets_and_preview(self):
        from openpyxl import Workbook
        book = Workbook()
        book.active.title = 'Инструкция'
        book.active.append(['Описание'])
        sheet = book.create_sheet('Продажи')
        sheet.append(['Код', 'Дата', 'Количество'])
        sheet.append(['0001', date(2026, 8, 10), 5])
        stream = io.BytesIO()
        book.save(stream)
        upload = inspect_file(stream.getvalue(), 'sales.xlsx')
        self.assertEqual(preview(upload)['sheets'][0]['sample'][0], ['0001', '2026-08-10', '5'])
        dataset = build_dataset(upload, self.options(upload))
        self.assertEqual(dataset['items'][0]['sales']['2026-08'], 5)

    def test_partner_subheaders_totals_and_unknown_pack(self):
        upload = self.parse('Номенклатура;Номенклатура.Код;Кратность;янв. 2026;февр. 2026\n;;;Количество;Количество\nЛампа;001;0;10;20\nИтого;;;10;20')
        dataset = build_dataset(upload, self.options(upload))
        self.assertEqual(len(dataset['items']), 1)
        self.assertEqual(dataset['diagnostics']['imported_rows'], 1)
        self.assertEqual(dataset['diagnostics']['skipped_header_rows'], 1)
        self.assertEqual(dataset['diagnostics']['skipped_total_rows'], 1)
        self.assertIsNone(dataset['items'][0]['pack'])

    def test_no_partial_import_on_invalid_rows(self):
        for invalid in ['001;2026-08-01;abc', '001;bad;3', '001;2027-01-01;3', ';2026-08-01;3', '001;2026-08-01;NaN']:
            upload = self.parse('Код;Дата;Количество\n001;2026-08-01;3\n'+invalid)
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'Строка 3'):
                build_dataset(upload, self.options(upload))

    def test_conflicting_snapshots_and_duplicate_monthly_products(self):
        for text in ['Код;Дата;Количество;Остаток\n001;2026-08-01;3;8\n001;2026-08-02;4;10',
                     'Код;2026-08\n001;3\n001;3']:
            upload = self.parse(text)
            with self.assertRaises(ValueError):
                build_dataset(upload, self.options(upload))

    def test_manual_mapping_and_validation(self):
        upload = self.parse('Product;When;Count\nABC;01.08.2026;10')
        with self.assertRaisesRegex(ValueError, 'Укажите столбец'):
            build_dataset(upload, self.options(upload))
        dataset = build_dataset(upload, self.options(upload, mapping={'code': 0, 'date': 1, 'qty': 2}))
        self.assertEqual(dataset['items'][0]['code'], 'ABC')
        with self.assertRaisesRegex(ValueError, 'нескольким полям'):
            build_dataset(upload, self.options(upload, mapping={'code': 0, 'date': 1, 'qty': 1}))

    def test_reject_empty_wrong_format_and_incomplete_history(self):
        for data, filename in [(b'', 'test.csv'), (b'broken', 'test.xlsx'), (b'hello', 'test.exe')]:
            with self.assertRaises(ValueError):
                inspect_file(data, filename)
        upload = self.parse('Код;Дата;Количество\n001;2026-09-01;5')
        with self.assertRaisesRegex(ValueError, 'завершённые месяцы'):
            build_dataset(upload, self.options(upload))

    def test_transit_snapshots_not_double_counted(self):
        upload = self.parse('Код;Дата;Количество;Остаток;В пути;Дата поступления\n001;2026-07-01;8;0;10;2026-10-01\n001;2026-08-01;8;0;10;2026-10-01')
        dataset = build_dataset(upload, self.options(upload))
        row = calculate_all(dataset, dict(app.DEFAULTS, as_of='2026-09-23'))[0]
        self.assertEqual(row['in_transit'], 10)


class UploadApiTests(unittest.TestCase):
    def test_upload_persistence_switching_errors_and_csrf(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'ROOT', Path(directory)):
            server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
            server.state, server.csrf = app.State(demo=True), 'test-token'
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f'http://127.0.0.1:{server.server_port}'
            def post(path, data, headers=None):
                req = Request(url+path, data=data, headers=headers or {'X-CSRF-Token': 'test-token', 'Content-Type': 'application/json'})
                with urlopen(req) as response:
                    return json.load(response)
            try:
                with self.assertRaises(HTTPError) as exc:
                    post('/api/import/preview', b'bad', {'X-CSRF-Token': 'wrong'})
                self.assertEqual(exc.exception.code, 403)
                before = copy.deepcopy(server.state.base)
                content = 'Код;Дата;Количество;Остаток\nA;2026-08-01;10;0'.encode('utf-8')
                result = post('/api/import/preview', content, {'X-CSRF-Token': 'test-token', 'X-File-Name': 'test.csv'})
                self.assertEqual(server.state.base, before)
                options = dict(token=result['token'], sheet=0, mapping=result['sheets'][0]['mapping'], mode='transactions', as_of='2026-09-23')
                invalid = dict(options, mapping={})
                with self.assertRaises(HTTPError):
                    post('/api/import/commit', json.dumps(invalid).encode())
                self.assertEqual(server.state.base, before)
                post('/api/import/commit', json.dumps(options).encode())
                self.assertEqual(server.state.mode, 'upload')
                self.assertEqual(app.State().base['items'][0]['code'], 'A')
                post('/api/dataset', b'{"mode":"demo"}')
                self.assertEqual(server.state.base, before)
                post('/api/dataset', b'{"mode":"upload"}')
                self.assertEqual(server.state.rows[0]['code'], 'A')
                with urlopen(url+'/api/import/template') as response:
                    self.assertIn('Код товара', response.read().decode('utf-8-sig'))
            finally:
                server.shutdown()
                thread.join(timeout=3)
                server.server_close()


if __name__ == '__main__':
    unittest.main()
