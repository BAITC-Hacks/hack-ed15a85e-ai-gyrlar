import copy
import csv
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app import State
from replenishment import orders
from replenishment.engine import month_range


def make_item(code, supplier='IEK'):
    return dict(id=supplier+':'+code, code=code, article='ART-'+code, name='Товар '+code,
                supplier=supplier, unit='шт', category='A', stock=0,
                stock_date='2026-09-22', pack=10, moq=20,
                sales={month: 100 for month in month_range(date(2026, 9, 1), 12)},
                transit=[], sources=[], warnings=[])


def make_data():
    return dict(version=1, as_of='2026-09-22', items=[make_item('001'), make_item('002')],
                seasonality={'IEK': [1.0]*12}, diagnostics={}, sources=[], limitations=[])


class OrderUnitTests(unittest.TestCase):
    def test_quantity_rejects_bad_numbers_pack_and_moq_but_preserves_zero(self):
        for value in (True, False, '20', None, -1, float('nan'), float('inf'), 10**9+1, 10, 21):
            with self.subTest(value=value), self.assertRaises(ValueError):
                orders.validate_quantity(value, pack=10, moq=20)
        self.assertEqual(orders.validate_quantity(0, pack=10, moq=20), 0)
        self.assertEqual(orders.validate_quantity(30, pack=10, moq=20), 30)

    def test_only_drafts_reserve_creation_quantities(self):
        documents = [dict(status=status, lines=[dict(item_id='I:1', qty=qty)])
                     for status, qty in [('draft', 20), ('draft', 30), ('approved', 100),
                                         ('received', 200), ('cancelled', 300)]]
        self.assertEqual(orders.reservations(documents), {'I:1': 50})
        self.assertEqual(orders.summary(documents), dict(draft=2, approved=1, received=1, cancelled=1))

    def test_external_reference_partial_supply_is_supplemented_once(self):
        for reference in ('order-internal-id', 'SP-0001'):
            with self.subTest(reference=reference):
                data = make_data()
                data['items'][0]['transit'] = [dict(qty=40, eta='2026-10-10', order_id=reference)]
                order = dict(id='order-internal-id', number='SP-0001', status='approved',
                             expected_at='2026-10-22', lines=[dict(item_id='IEK:001', qty=100)])
                orders.apply_commitments(data, [order])
                self.assertEqual(sum(t['qty'] for t in data['items'][0]['transit']), 100)
                self.assertEqual(data['items'][0]['transit'][-1]['qty'], 60)
                orders.apply_commitments(data, [order])
                self.assertEqual(sum(t['qty'] for t in data['items'][0]['transit']), 100)

    def test_unrelated_external_supply_preserved_and_closed_order_removed(self):
        for status in ('received', 'cancelled'):
            with self.subTest(status=status):
                data = make_data()
                data['items'][0]['transit'] = [dict(qty=100, eta='2026-10-22', order_id='SP-0001'),
                                             dict(qty=50, eta='2026-10-22', order_id='other-order')]
                order = dict(id='id1', number='SP-0001', status=status, lines=[dict(item_id='IEK:001', qty=100)])
                orders.apply_commitments(data, [order])
                self.assertEqual(data['items'][0]['transit'], [dict(qty=50, eta='2026-10-22', order_id='other-order')])


class OrderStateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root_patch = patch('app.ROOT', Path(self.folder.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.state = State(mode='upload', dataset=make_data())

    def row(self, key='IEK:001'):
        return next(row for row in self.state.rows if row['id'] == key)

    def create(self, ids=None):
        return self.state.create_orders(dict(ids=ids or ['IEK:001']))['orders'][0]

    def approve(self, order):
        return self.state.order_status(dict(id=order['id'], status='approved', confirmed=True))

    def changes(self, order):
        return dict(id=order['id'], expected_at=order['expected_at'], note='Проверено',
                    lines=[dict(item_id=line['item_id'], qty=line['qty']) for line in order['lines']])

    def test_second_draft_does_not_order_same_requirement_again(self):
        recommended = self.row()['qty']
        order = self.create(['IEK:001', 'IEK:001'])
        self.assertEqual(len(order['lines']), 1)
        self.assertEqual(order['lines'][0]['qty'], recommended)
        self.assertEqual(self.row()['draft_qty'], recommended)
        self.assertEqual(self.row()['available_to_order'], 0)
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(len(self.state.orders), 1)
        self.assertEqual(self.row()['in_transit'], 0)

    def test_second_draft_only_adds_uncovered_quantity_after_manual_reduction(self):
        original = self.row()['qty']
        order = self.create()
        changes = self.changes(order)
        changes['lines'][0]['qty'] = 20
        self.state.update_order(changes)
        second = self.create()
        self.assertEqual(second['lines'][0]['qty'], original-20)
        self.assertEqual(self.row()['draft_qty'], original)
        with self.assertRaises(ValueError):
            self.create()

    def test_approval_becomes_transit_and_cannot_be_approved_twice(self):
        order = self.create()
        qty = order['lines'][0]['qty']
        self.approve(order)
        self.assertEqual(order['status'], 'approved')
        self.assertEqual(self.row()['draft_qty'], 0)
        self.assertEqual(self.row()['in_transit'], qty)
        self.assertEqual(self.row()['qty'], 0)
        with self.assertRaises(ValueError):
            self.approve(order)
        with self.assertRaises(ValueError):
            self.create()
        self.state.recalculate()
        self.assertEqual(self.row()['in_transit'], qty)

    def test_external_order_reference_reconciles_without_double_counting(self):
        order = self.create()
        qty = order['lines'][0]['qty']
        self.approve(order)
        self.state.base['items'][0]['transit'] = [dict(qty=qty, eta=order['expected_at'], order_id=order['number'])]
        self.state.recalculate()
        self.assertEqual(self.row()['in_transit'], qty)
        self.assertEqual(len(self.row()['transit']), 1)
        self.state.base['items'][0]['transit'][0]['qty'] = qty-20
        self.state.recalculate()
        self.assertEqual(self.row()['in_transit'], qty)
        self.assertEqual(sum(t['qty'] for t in self.row()['transit'] if t.get('internal')), 20)

    def test_receive_updates_stock_once_removes_transit_and_persists(self):
        order = self.create()
        qty = order['lines'][0]['qty']
        self.approve(order)
        self.state.base['items'][0]['transit'] = [dict(qty=qty, eta=order['expected_at'], order_id=order['number'])]
        self.state.recalculate()
        self.state.order_status(dict(id=order['id'], status='received', confirmed=True, stock_confirmed=True))
        self.assertEqual(self.row()['stock'], qty)
        self.assertEqual(self.row()['stock_date'], '2026-09-22')
        self.assertEqual(self.row()['in_transit'], 0)
        self.assertEqual(self.row()['total_transit'], 0)
        with self.assertRaises(ValueError):
            self.state.order_status(dict(id=order['id'], status='received', confirmed=True, stock_confirmed=True))
        self.assertEqual(self.row()['stock'], qty)
        reloaded = State(mode='upload')
        persisted = next(r for r in reloaded.rows if r['id'] == 'IEK:001')
        self.assertEqual(persisted['stock'], qty)
        self.assertEqual(persisted['in_transit'], 0)
        self.assertEqual(reloaded.orders[0]['status'], 'received')

    def test_receive_needs_stock_confirmation_and_known_stock(self):
        order = self.create()
        self.approve(order)
        with self.assertRaises(ValueError):
            self.state.order_status(dict(id=order['id'], status='received', confirmed=True))
        self.state.overrides['IEK:001'] = dict(stock=None)
        self.state.recalculate()
        with self.assertRaises(ValueError):
            self.state.order_status(dict(id=order['id'], status='received', confirmed=True, stock_confirmed=True))
        self.assertEqual(order['status'], 'approved')

    def test_confirmation_requires_json_boolean_true(self):
        order = self.create()
        for value in ('false', 'true', 1, 0, [], {}):
            with self.subTest(confirmed=value), self.assertRaises(ValueError):
                self.state.order_status(dict(id=order['id'], status='approved', confirmed=value))
        self.assertEqual(order['status'], 'draft')
        self.approve(order)
        for value in ('false', 'true', 1, 0, [], {}):
            with self.subTest(stock_confirmed=value), self.assertRaises(ValueError):
                self.state.order_status(dict(id=order['id'], status='received', confirmed=True, stock_confirmed=value))
        self.assertEqual(order['status'], 'approved')
        self.assertEqual(self.row()['stock'], 0)

    def test_bad_quantity_update_is_atomic_in_memory_and_on_disk(self):
        order = self.create(['IEK:001', 'IEK:002'])
        before = copy.deepcopy(self.state.orders)
        saved = self.state.path.read_bytes()
        changes = self.changes(order)
        changes['lines'][0]['qty'] += 10
        changes['lines'][1]['qty'] = 21
        with self.assertRaises(ValueError):
            self.state.update_order(changes)
        self.assertEqual(self.state.orders, before)
        self.assertEqual(self.state.path.read_bytes(), saved)

    def test_failed_save_rolls_back_status_transit_and_notifications(self):
        order = self.create()
        before = copy.deepcopy(self.state.orders)
        notices = copy.deepcopy(self.state.notifications)
        with patch.object(self.state, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.approve(order)
        self.assertEqual(self.state.orders, before)
        self.assertEqual(self.state.notifications, notices)
        self.assertEqual(self.row()['in_transit'], 0)
        self.assertEqual(self.row()['draft_qty'], order['lines'][0]['qty'])

    def test_approval_rejects_stale_stock_even_after_draft_refresh(self):
        self.state.overrides['IEK:001'] = dict(stock_date='2026-09-01')
        self.state.recalculate()
        order = self.create()
        self.state.update_order(self.changes(order))
        with self.assertRaises(ValueError):
            self.approve(order)
        self.assertEqual(order['status'], 'draft')

    def test_backdated_calculation_cannot_approve_future_stock_snapshot(self):
        self.state.settings['as_of'] = '2026-09-01'
        self.state.recalculate()
        self.assertEqual(self.row()['stock_date'], '2026-09-22')
        self.assertEqual(self.row()['status'], 'Предварительно')
        self.assertTrue(any('позже даты расчёта' in warning for warning in self.row()['warnings']))
        order = self.create()
        self.state.update_order(self.changes(order))
        with self.assertRaises(ValueError):
            self.approve(order)
        self.assertEqual(order['status'], 'draft')

    def test_known_stock_without_snapshot_date_cannot_be_approved(self):
        self.state.overrides['IEK:001'] = dict(stock=0, stock_date=None)
        self.state.recalculate()
        self.assertEqual(self.row()['status'], 'Предварительно')
        order = self.create()
        self.state.update_order(self.changes(order))
        with self.assertRaises(ValueError):
            self.approve(order)
        self.assertEqual(order['status'], 'draft')

    def test_changed_calculation_must_be_reviewed_before_approval(self):
        order = self.create()
        self.state.overrides['IEK:001'] = dict(stock=20)
        self.state.recalculate()
        with self.assertRaises(ValueError):
            self.approve(order)
        self.state.update_order(self.changes(order))
        self.approve(order)
        self.assertEqual(order['status'], 'approved')

    def test_cancellation_releases_draft_reservation_or_approved_transit(self):
        for approved in (False, True):
            with self.subTest(approved=approved):
                self.state = State(mode='upload', dataset=make_data())
                order = self.create()
                if approved:
                    self.approve(order)
                self.state.order_status(dict(id=order['id'], status='cancelled', confirmed=True))
                self.assertEqual(self.row()['in_transit'], 0)
                self.assertEqual(self.row()['draft_qty'], 0)
                self.assertGreater(self.row()['available_to_order'], 0)
                self.create()

    def test_csv_export_preserves_text_identifiers_and_defuses_formulas(self):
        order = self.create()
        order['supplier'] = '=HYPERLINK("x")'
        order['lines'][0]['name'] = '@SUM(A1:A2)'
        data, content_type = orders.export_order(order, 'csv')
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        self.assertIn('text/csv', content_type)
        parsed = list(csv.reader(io.StringIO(data.decode('utf-8-sig')), delimiter=';'))
        self.assertTrue(parsed[0][3].startswith("'="))
        self.assertEqual(parsed[3][0], '001')
        self.assertTrue(parsed[3][2].startswith("'@"))
        self.assertEqual(float(parsed[3][3]), order['lines'][0]['qty'])


if __name__ == '__main__':
    unittest.main()
