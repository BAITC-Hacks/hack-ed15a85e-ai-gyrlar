"""Public HTTP flows use isolated application data and an ephemeral local port."""
import copy
import io
import json
import tempfile
import threading
import unittest
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
from replenishment.engine import month_range


class QuietHandler(app.Handler):
    def log_message(self, format, *args):
        pass


def sales_csv(value=100, spike=False, codes=('001',)):
    months = month_range(date(2026, 9, 1), 12)
    lines = ['Код товара;Поставщик;Наименование;Единица;'+ ';'.join(months)]
    for code in codes:
        quantities = [str(value)]*len(months)
        if spike:
            quantities[-1] = '10000'
        lines.append(f'{code};IEK;Товар {code};шт;'+ ';'.join(quantities))
    return '\n'.join(lines)


def embedded_stock_csv(stock=20, pack=5, codes=('001',)):
    lines = sales_csv(codes=codes).splitlines()
    return '\n'.join([lines[0]+';Остаток;Дата остатка;Кратность']+
                     [line+f';{stock};2026-09-22;{pack}' for line in lines[1:]])


STOCK = 'Код товара;Поставщик;Остаток;Дата остатка;Кратность;Минимальная партия;Единица\n001;IEK;20;2026-09-22;5;10;шт'
TRANSIT = 'Код товара;Поставщик;В пути;Дата поступления;Номер заказа;Единица\n001;IEK;20;2026-09-25;EXT-1;шт\n001;IEK;10;2026-10-05;EXT-2;шт'


class WorkflowApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        root_patch = patch.object(app, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.server.state = app.State(demo=True)
        self.server.csrf = 'workflow-test-token'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs=dict(poll_interval=0.02), daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def call(self, path, body=None, headers=None, expected=200, binary=False):
        request_headers = {'X-CSRF-Token': self.server.csrf, 'Content-Type': 'application/json'}
        request_headers.update(headers or {})
        data = json.dumps(body, ensure_ascii=False).encode('utf-8') if isinstance(body, dict) else body
        request = Request(self.url+path, data=data, headers=request_headers)
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            self.assertEqual(response.status, expected, content.decode('utf-8', errors='replace')[:1000] if response.status != 200 else '')
            return (content, response.headers) if binary else json.loads(content)

    def upload(self, kind, content, expected=200):
        result = self.call('/api/import/preview', content.encode('utf-8-sig'),
                           headers={'X-File-Name': kind+'.csv', 'X-Import-Kind': kind})
        sheet = result['sheets'][0]
        return self.call('/api/import/commit', dict(token=result['token'], kind=kind,
                         sheet=0, mode=sheet['mode'], mapping=sheet['mapping'], as_of='2026-09-22'), expected=expected)

    def row(self):
        return self.call('/api/state')['rows'][0]

    def import_three(self, spike=False):
        self.upload('sales', sales_csv(spike=spike))
        self.upload('stock', STOCK)
        self.upload('transit', TRANSIT)
        return self.row()

    def test_complete_sales_stock_transit_order_excel_and_receipt(self):
        self.upload('sales', sales_csv())
        sales = self.row()
        self.assertIsNone(sales['stock'])
        self.assertIsNone(sales['qty'])
        self.upload('stock', STOCK)
        stocked = self.row()
        self.assertEqual(stocked['stock'], 20)
        self.assertGreater(stocked['qty'], 0)
        self.upload('transit', TRANSIT)
        row = self.row()
        self.assertEqual(row['in_transit'], 30)
        self.assertLess(row['qty'], stocked['qty'])
        state = self.call('/api/state')
        self.assertEqual(set(state['source_imports']), {'sales', 'stock', 'transit'})
        order = self.call('/api/orders/create', {'ids': [row['id']]})['orders'][0]
        self.call('/api/orders/create', {'ids': [row['id']]}, expected=400)
        qty = order['lines'][0]['qty']+5
        order = self.call('/api/orders/update', dict(id=order['id'], expected_at=order['expected_at'],
                          note='Проверено менеджером', lines=[dict(item_id=row['id'], qty=qty)]))
        self.assertEqual(order['lines'][0]['qty'], qty)
        approved = self.call('/api/orders/status', dict(id=order['id'], status='approved', confirmed=True))
        self.assertEqual(approved['status'], 'approved')
        self.assertEqual(self.row()['in_transit'], qty+30)
        content, headers = self.call('/api/orders/export', dict(id=order['id'], format='xlsx'), binary=True)
        self.assertIn('spreadsheetml.sheet', headers['Content-Type'])
        self.assertTrue(content.startswith(b'PK'))
        from openpyxl import load_workbook
        book = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
        try:
            self.assertEqual(book.active['A6'].value, '001')
            self.assertEqual(book.active['D6'].value, qty)
            self.assertEqual(book.active['B2'].value, 'IEK')
            self.assertEqual(book.active['B4'].value, 'Проверено менеджером')
        finally:
            book.close()
        received = self.call('/api/orders/status', dict(id=order['id'], status='received', confirmed=True, stock_confirmed=True))
        self.assertEqual(received['status'], 'received')
        self.assertEqual(self.row()['stock'], 20+qty)
        self.assertEqual(self.row()['in_transit'], 30)
        self.call('/api/orders/status', dict(id=order['id'], status='received', confirmed=True, stock_confirmed=True), expected=400)
        self.assertEqual(self.row()['stock'], 20+qty)
        persisted = app.State(mode='upload')
        self.assertEqual(persisted.rows[0]['stock'], 20+qty)
        self.assertEqual(persisted.orders[0]['status'], 'received')

    def test_anomaly_and_backtest_routes_are_transparent_and_validate_input(self):
        row = self.import_three(spike=True)
        anomalies = self.call('/api/anomalies?limit=1&offset=0')
        self.assertEqual(anomalies['summary']['candidate_months'], 1)
        candidate = anomalies['items'][0]
        self.assertEqual(candidate['month'], '2026-08')
        self.assertEqual(candidate['regular'], 100)
        self.assertGreater(candidate['order_before'], candidate['order_after'])
        clean_qty = row['qty']
        self.call('/api/anomalies/decision', dict(id=row['id'], month='2026-08', decision='keep'))
        self.assertGreater(self.row()['qty'], clean_qty)
        self.assertEqual(self.call('/api/anomalies')['items'][0]['decision'], 'keep')
        self.call('/api/anomalies/decision', dict(id=row['id'], month='2026-07', decision='exclude'), expected=400)
        self.call('/api/anomalies/decision', dict(id=row['id'], month='2026-08', decision='exclude'))
        self.assertEqual(self.row()['qty'], clean_qty)
        self.call('/api/anomalies?limit=bad', expected=400)
        before = copy.deepcopy(self.server.state.base)
        result = self.call('/api/backtest', {'months': 3})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['samples'], 3)
        self.assertEqual(result['per_unit'][0]['unit'], 'шт')
        self.assertGreater(result['per_unit'][0]['model']['mae'], 0)
        self.assertTrue(result['methodology'])
        self.assertTrue(result['limitations'])
        self.assertEqual(self.server.state.base, before)
        for invalid in (0, 7, '3', True):
            self.call('/api/backtest', {'months': invalid}, expected=400)

    def test_refresh_sales_preserves_separate_sources_ids_and_order_history(self):
        initial = self.import_three()
        order = self.call('/api/orders/create', {'ids': [initial['id']]})['orders'][0]
        previous_transit = copy.deepcopy(self.server.state.base['items'][0]['transit'])
        self.upload('sales', sales_csv(value=150))
        updated = self.row()
        self.assertEqual(updated['id'], initial['id'])
        self.assertEqual(updated['stock'], 20)
        self.assertEqual(updated['pack'], 5)
        self.assertEqual(updated['in_transit'], 30)
        self.assertEqual(self.server.state.base['items'][0]['transit'], previous_transit)
        self.assertGreater(updated['demand'], initial['demand'])
        self.assertEqual(self.call('/api/orders')['orders'][0]['id'], order['id'])
        self.call('/api/orders/status', dict(id=order['id'], status='approved', confirmed=True), expected=400)

    def test_new_stock_snapshot_replaces_manual_receipt_adjustment(self):
        row = self.import_three()
        order = self.call('/api/orders/create', {'ids': [row['id']]})['orders'][0]
        self.call('/api/orders/status', dict(id=order['id'], status='approved', confirmed=True))
        self.call('/api/orders/status', dict(id=order['id'], status='received', confirmed=True, stock_confirmed=True))
        self.assertGreater(self.row()['stock'], 20)
        self.upload('stock', STOCK.replace(';20;', ';250;'))
        self.assertEqual(self.row()['stock'], 250)
        self.assertNotIn('stock', self.server.state.overrides.get(row['id'], {}))
        self.assertEqual(self.call('/api/orders')['orders'][0]['status'], 'received')

    def test_embedded_stock_refresh_also_replaces_receipt_override(self):
        self.upload('sales', embedded_stock_csv())
        row = self.row()
        order = self.call('/api/orders/create', {'ids': [row['id']]})['orders'][0]
        self.call('/api/orders/status', dict(id=order['id'], status='approved', confirmed=True))
        self.call('/api/orders/status', dict(id=order['id'], status='received', confirmed=True, stock_confirmed=True))
        self.assertGreater(self.row()['stock'], 20)
        self.upload('sales', embedded_stock_csv(stock=250, pack=10))
        self.assertEqual(self.row()['stock'], 250)
        self.assertEqual(self.row()['pack'], 10)
        self.assertNotIn('stock', self.server.state.overrides.get(row['id'], {}))
        self.assertEqual(self.call('/api/orders')['orders'][0]['status'], 'received')

    def test_refresh_keeps_missing_products_with_separate_stock_or_transit(self):
        row = self.import_three()
        self.upload('sales', sales_csv(codes=('002',)).replace('IEK', 'Other Supplier'))
        rows = self.call('/api/state')['rows']
        self.assertEqual(len(rows), 2)
        retained = next(r for r in rows if r['id'] == row['id'])
        self.assertEqual(retained['stock'], 20)
        self.assertEqual(retained['in_transit'], 30)
        self.assertEqual(retained['monthly'], 0)
        self.assertEqual(retained['qty'], 0)
        self.assertTrue(any('отсутствует в новой истории' in warning for warning in retained['warnings']))
        self.assertIn('IEK', self.server.state.base['seasonality'])

    def test_refresh_cannot_remove_active_order_products(self):
        self.upload('sales', embedded_stock_csv(stock=0))
        row = self.row()
        order = self.call('/api/orders/create', {'ids': [row['id']]})['orders'][0]
        before = copy.deepcopy(self.server.state.base)
        self.upload('sales', embedded_stock_csv(stock=0, codes=('002',)), expected=400)
        self.assertEqual(self.server.state.base, before)
        self.assertEqual(self.call('/api/orders')['orders'][0]['id'], order['id'])
        self.call('/api/orders/status', dict(id=order['id'], status='cancelled', confirmed=True))
        self.upload('sales', embedded_stock_csv(stock=0, codes=('002',)))
        self.assertEqual(self.row()['code'], '002')

    def test_backtest_short_history_returns_unavailable_not_zero_error(self):
        self.upload('sales', 'Код товара;Поставщик;2026-08\n001;IEK;100')
        result = self.call('/api/backtest', {'months': 3})
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['samples'], 0)
        self.assertEqual(result['per_unit'], [])
        self.assertEqual(result['skipped'], dict(missing_actual=2, insufficient_history=1))

    def test_folder_automation_bad_source_cancels_batch_and_keeps_hashes(self):
        self.import_three()
        watch = self.root/'incoming'
        watch.mkdir()
        (watch/'sales.csv').write_text(sales_csv(value=150), encoding='utf-8-sig')
        (watch/'stock.csv').write_text(STOCK.replace(';20;', ';bad;'), encoding='utf-8-sig')
        (watch/'transit.csv').write_text(TRANSIT, encoding='utf-8-sig')
        self.call('/api/automation', dict(enabled=True, interval_minutes=60, advance_date=False, folder=str(watch)))
        before = copy.deepcopy(self.server.state.base)
        rows = copy.deepcopy(self.server.state.rows)
        hashes = copy.deepcopy(self.server.state.automation['hashes'])
        self.call('/api/automation/run', {}, expected=400)
        self.assertEqual(self.server.state.base, before)
        self.assertEqual(self.server.state.rows, rows)
        self.assertEqual(self.server.state.automation['hashes'], hashes)
        self.assertTrue(self.server.state.automation['last_error'])
        restored = app.State(mode='upload')
        self.assertEqual(restored.base, before)
        self.assertTrue(restored.automation['last_error'])
        (watch/'stock.csv').write_text(STOCK.replace(';20;', ';40;'), encoding='utf-8-sig')
        result = self.call('/api/automation/run', {})
        self.assertEqual(set(result['changed_sources']), {'sales', 'stock', 'transit'})
        self.assertIsNone(result['automation']['last_error'])
        self.assertEqual(set(result['automation']['hashes']), {'sales', 'stock', 'transit'})
        self.assertEqual(self.row()['stock'], 40)
        self.assertGreater(self.row()['demand'], rows[0]['demand'])
        notices = len(self.server.state.notifications)
        result = self.call('/api/automation/run', {})
        self.assertEqual(result['changed_sources'], [])
        self.assertFalse(result['automation']['changed'])
        self.assertEqual(len(self.server.state.notifications), notices)

    def test_folder_sales_cannot_overwrite_demo_with_real_data(self):
        before = copy.deepcopy(self.server.state.base)
        watch = self.root/'incoming'
        watch.mkdir()
        (watch/'sales.csv').write_text(sales_csv(), encoding='utf-8-sig')
        self.call('/api/automation', dict(enabled=True, interval_minutes=60, advance_date=False, folder=str(watch)))
        error = self.call('/api/automation/run', {}, expected=400)
        self.assertTrue(error['error'])
        self.assertEqual(self.server.state.mode, 'demo')
        self.assertEqual(self.server.state.base, before)
        self.assertEqual(self.server.state.automation['hashes'], {})
        self.assertEqual(app.State(demo=True).base, before)
        self.assertFalse((self.root/'data'/'uploaded-state.json').exists())

    def test_folder_sales_cannot_remove_active_order_products(self):
        self.upload('sales', embedded_stock_csv(stock=0))
        row = self.row()
        order = self.call('/api/orders/create', {'ids': [row['id']]})['orders'][0]
        before = copy.deepcopy(self.server.state.base)
        watch = self.root/'incoming'
        watch.mkdir()
        (watch/'sales.csv').write_text(embedded_stock_csv(stock=0, codes=('002',)), encoding='utf-8-sig')
        self.call('/api/automation', dict(enabled=True, interval_minutes=60, advance_date=False, folder=str(watch)))
        self.call('/api/automation/run', {}, expected=400)
        self.assertEqual(self.server.state.base, before)
        self.assertEqual(self.server.state.automation['hashes'], {})
        self.assertEqual(self.call('/api/orders')['orders'][0]['id'], order['id'])

    def test_import_from_demo_reuses_existing_uploaded_order_ledger(self):
        original = self.import_three()
        order = self.call('/api/orders/create', {'ids': [original['id']]})['orders'][0]
        self.call('/api/orders/status', dict(id=order['id'], status='approved', confirmed=True))
        self.call('/api/dataset', {'mode': 'demo'})
        self.assertEqual(self.call('/api/state')['mode'], 'demo')
        self.upload('sales', sales_csv(value=150))
        result = self.call('/api/state')
        self.assertEqual(result['mode'], 'upload')
        self.assertEqual(result['rows'][0]['stock'], 20)
        self.assertEqual(set(result['source_imports']), {'sales', 'stock', 'transit'})
        self.assertEqual(result['rows'][0]['in_transit'], 30+order['lines'][0]['qty'])
        restored = self.call('/api/orders')['orders']
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]['id'], order['id'])
        self.assertEqual(restored[0]['status'], 'approved')

    def test_rejected_import_from_demo_preserves_active_selection_and_saved_ledger(self):
        self.upload('sales', embedded_stock_csv(stock=0))
        row = self.row()
        order = self.call('/api/orders/create', {'ids': [row['id']]})['orders'][0]
        saved_path = self.root/'data'/'uploaded-state.json'
        before = saved_path.read_bytes()
        self.call('/api/dataset', {'mode': 'demo'})
        self.upload('sales', embedded_stock_csv(stock=0, codes=('002',)), expected=400)
        self.assertEqual(self.call('/api/state')['mode'], 'demo')
        self.assertEqual(saved_path.read_bytes(), before)
        self.call('/api/dataset', {'mode': 'upload'})
        self.assertEqual(self.row()['id'], row['id'])
        self.assertEqual(self.call('/api/orders')['orders'][0]['id'], order['id'])

    def test_sales_unit_change_cannot_relabel_preserved_stock_and_transit(self):
        row = self.import_three()
        before = copy.deepcopy(self.server.state.base)
        self.upload('sales', sales_csv().replace(';шт;', ';уп;'), expected=400)
        self.assertEqual(self.server.state.base, before)
        self.assertEqual(self.row()['unit'], 'шт')
        self.assertEqual(self.row()['stock'], row['stock'])
        self.assertEqual(self.row()['in_transit'], row['in_transit'])

    def test_sales_without_unit_keeps_existing_unit_for_preserved_sources(self):
        row = self.import_three()
        without_unit = sales_csv().replace(';Единица;', ';').replace(';шт;', ';')
        self.upload('sales', without_unit)
        self.assertEqual(self.row()['id'], row['id'])
        self.assertEqual(self.row()['unit'], 'шт')
        self.assertEqual(self.row()['stock'], row['stock'])
        self.assertEqual(self.row()['in_transit'], row['in_transit'])

    def test_later_sales_row_fills_blank_unit_and_conflict_is_rejected(self):
        content = ('Код товара;Поставщик;Дата продажи;Количество продано;Единица\n'
                   '001;IEK;2026-01-10;10;\n001;IEK;2026-02-10;10;шт')
        self.upload('sales', content)
        self.assertEqual(self.row()['unit'], 'шт')
        before = copy.deepcopy(self.server.state.base)
        self.upload('sales', content+'\n001;IEK;2026-03-10;10;уп', expected=400)
        self.assertEqual(self.server.state.base, before)
        self.assertEqual(self.row()['unit'], 'шт')


if __name__ == '__main__':
    unittest.main()
