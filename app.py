"""Local, single-user MVP. Start: python app.py; open http://127.0.0.1:8765."""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import math
import mimetypes
import os
import secrets
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

from replenishment.agent import ask, summary, ToolCache
from replenishment.demo import make_demo
from replenishment.engine import calculate_all, number
from replenishment.uploads import MAX_BYTES, inspect_file, preview, build_dataset, merge_source, template_bytes
from replenishment import orders as purchase_orders

ROOT = Path(__file__).resolve().parent
DEFAULTS = dict(as_of='2026-09-22', lead_days=30, review_days=14, safety_days=7, growth_pct=0,
                seasonality=True, trend=True, remove_outliers=True, compensate_stockouts=True, category_policies={}, supplier_policies={})


def load_env():
    path = ROOT/'.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                if key.strip() in ['OPENAI_API_KEY', 'OPENAI_MODEL']:
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def validate_settings(value):
    result = copy.deepcopy(DEFAULTS)
    if not isinstance(value, dict):
        raise ValueError('Неверные параметры расчёта')
    result['as_of'] = date.fromisoformat(value.get('as_of', DEFAULTS['as_of'])).isoformat()
    for key, (lo, hi) in {'lead_days': (1, 180), 'review_days': (1, 90), 'safety_days': (0, 90), 'growth_pct': (-90, 300)}.items():
        v = value.get(key, result[key])
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
            raise ValueError(f'Параметр {key}: допустимо от {lo} до {hi}')
        if key != 'growth_pct' and int(v) != v:
            raise ValueError('Сроки должны быть целым числом дней')
        result[key] = v
    for key in ['seasonality', 'trend', 'remove_outliers', 'compensate_stockouts']:
        if key in value and not isinstance(value[key], bool):
            raise ValueError('Переключатель должен быть логическим значением')
        result[key] = value.get(key, result[key])
    policies = value.get('category_policies', {})
    if not isinstance(policies, dict) or len(policies) > 50:
        raise ValueError('Неверные правила категорий')
    for key, policy in policies.items():
        if not isinstance(policy, dict) or set(policy)-{'lead_days', 'safety_days', 'growth_pct'}:
            raise ValueError('Правила категории: lead_days, safety_days, growth_pct')
        validate_settings({**{k: v for k, v in policy.items()}, 'category_policies': {}})
        if not -90 <= result['growth_pct']+policy.get('growth_pct', 0) <= 300:
            raise ValueError('Общий прирост с правилом категории должен быть от −90% до 300%')
    result['category_policies'] = policies
    suppliers = value.get('supplier_policies', {})
    if not isinstance(suppliers, dict) or len(suppliers) > 200:
        raise ValueError('Неверные условия поставщиков')
    for supplier, policy in suppliers.items():
        if not isinstance(supplier, str) or not isinstance(policy, dict) or set(policy)-{'lead_days', 'review_days', 'safety_days'}:
            raise ValueError('Условия поставщика: срок поставки, цикл заказа и страховой запас')
        for field, val in policy.items():
            low, high = {'lead_days': (1, 180), 'review_days': (1, 90), 'safety_days': (0, 90)}[field]
            if type(val) is not int or not low <= val <= high:
                raise ValueError(f'{supplier}: {field} должен быть целым числом от {low} до {high}')
    result['supplier_policies'] = suppliers
    return result


def fingerprint(row, settings):
    payload = {k: v for k, v in row.items() if k not in ['review', 'sources', 'draft_qty', 'available_to_order', 'create_qty']}
    return hashlib.sha256(json.dumps([payload, settings], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def csv_bytes(rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, delimiter=';')
    fields = [('supplier', 'Поставщик'), ('code', 'Код 1С'), ('article', 'Артикул'), ('name', 'Наименование'),
              ('unit', 'Единица'), ('qty', 'Рекомендация'), ('final_qty', 'Количество после проверки'),
              ('stock', 'Остаток'), ('stock_date', 'Дата остатка'), ('in_transit', 'В пути в горизонте'),
              ('demand', 'Потребность'), ('safety', 'Страховой запас'), ('urgency', 'Срочность'),
              ('status', 'Статус расчёта'), ('review_status', 'Проверка'), ('reason', 'Обоснование')]
    writer.writerow([label for _, label in fields])
    for row in rows:
        review = row.get('review') or {}
        data = dict(row, final_qty=review.get('qty', row['qty']), review_status='Утверждено локально' if review else 'Не утверждено')
        values = []
        for key, _ in fields:
            v = data.get(key)
            if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
                v = "'"+v
            values.append('' if v is None else v)
        writer.writerow(values)
    return ('\ufeff'+stream.getvalue()).encode('utf-8')


def refresh_sales(previous, dataset):
    """Preserve separately supplied snapshots when replacing sales history."""
    old = {(i['supplier'], i['code']): i for i in previous['items']}
    imports = previous.get('source_imports', {})
    for item in dataset['items']:
        prior = old.get((item['supplier'], item['code']))
        if prior:
            if item.get('unit') and prior.get('unit') and item['unit'] != prior['unit']:
                raise ValueError(f'{item["code"]}: единица в новой истории «{item["unit"]}» отличается от прежней «{prior["unit"]}». Приведите продажи, остатки и поставки к одной единице.')
            if not item.get('unit'):
                item['unit'] = prior.get('unit', '')
            item['id'] = prior['id']
            if any(kind in imports for kind in ('stock', 'transit')):
                item['sources'] = list(dict.fromkeys(item.get('sources', [])+prior.get('sources', [])))
                item['warnings'] = list(dict.fromkeys(item.get('warnings', [])+prior.get('warnings', [])))
        for kind, fields in [('stock', ('stock', 'stock_date', 'pack', 'moq')), ('transit', ('transit',))]:
            if kind not in imports:
                continue
            if prior:
                for field in fields:
                    if field in prior:
                        item[field] = copy.deepcopy(prior[field])
            elif kind == 'stock':
                item['stock'], item['stock_date'] = None, None
            else:
                item['transit'] = []
    existing = {(i['supplier'], i['code']) for i in dataset['items']}
    for identity, prior in old.items():
        if identity not in existing and any(kind in imports for kind in ('stock', 'transit')) and (number(prior.get('stock')) > 0 or prior.get('transit')):
            item = copy.deepcopy(prior)
            item.update(sales={}, outlier_removed={}, anomalies=[], stockout_days={})
            item.setdefault('warnings', []).append('Товар отсутствует в новой истории продаж. Сохранён для учёта остатка и поставок; прогноз не рассчитан.')
            dataset['items'].append(item)
            dataset['seasonality'].setdefault(item['supplier'], copy.deepcopy(previous['seasonality'].get(item['supplier'], [1.0]*12)))
    for kind in ('stock', 'transit'):
        if kind in imports:
            dataset.setdefault('source_imports', {})[kind] = copy.deepcopy(imports[kind])
            dataset['sources'].extend(copy.deepcopy(s) for s in previous.get('sources', []) if s.get('kind', s.get('type')) == kind)
    return dataset


class State:
    def __init__(self, demo=False, mode=None, dataset=None):
        self.lock = threading.RLock()
        self.pending = None
        if mode is None:
            mode = 'demo' if demo else 'partner'
            active = ROOT/'data/active-dataset.json'
            if not demo and active.exists():
                mode = json.loads(active.read_text(encoding='utf-8')).get('mode', mode)
        if mode == 'partner' and not (ROOT/'data/dataset.json').exists():
            mode = 'demo'
        if mode not in ('demo', 'partner', 'upload'):
            raise ValueError('Неизвестный набор данных')
        self.mode, self.demo = mode, mode == 'demo'
        self.path = ROOT/'data'/dict(demo='demo-state.json', partner='local-state.json', upload='uploaded-state.json')[mode]
        saved = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() and dataset is None else {}
        self.origin_signature = None if mode != 'partner' else hashlib.sha256((ROOT/'data/dataset.json').read_bytes()).hexdigest()
        self.custom_base = mode == 'upload' or bool(saved.get('dataset') and saved.get('origin_signature') == self.origin_signature)
        self.base = dataset or (saved.get('dataset') if self.custom_base else None)
        if not self.base and mode != 'upload':
            self.base = make_demo() if self.demo else json.loads((ROOT/'data/dataset.json').read_text(encoding='utf-8'))
        if not self.base:
            raise ValueError('Сначала загрузите файл с продажами')
        self.settings = copy.deepcopy(DEFAULTS)
        self.settings['as_of'] = self.base.get('as_of', DEFAULTS['as_of'])
        self.overrides, self.reviews = {}, {}
        self.orders, self.anomaly_decisions, self.notifications = [], {}, []
        self.automation = dict(enabled=False, interval_minutes=60, advance_date=False, folder='',
                               last_run=None, next_run=None, last_error=None, changed=False, hashes={})
        if saved:
            self.settings = validate_settings({**self.settings, **saved.get('settings', {})})
            self.overrides = saved.get('overrides', {})
            self.reviews = saved.get('reviews', {})
            self.orders = saved.get('orders', [])
            self.anomaly_decisions = saved.get('anomaly_decisions', {})
            self.automation.update(saved.get('automation', {}))
            self.notifications = saved.get('notifications', [])
        self.recalculate()

    def recalculate(self):
        self.revision = secrets.token_hex(12)
        self.agent_cache = ToolCache()
        self.dataset = copy.deepcopy(self.base)
        for item in self.dataset['items']:
            item.update(self.overrides.get(item['id'], {}))
            item['anomaly_decisions'] = {m: v['decision'] for m, v in self.anomaly_decisions.get(item['id'], {}).items()}
        purchase_orders.apply_commitments(self.dataset, self.orders)
        self.rows = calculate_all(self.dataset, self.settings)
        reserved = purchase_orders.reservations(self.orders)
        for row in self.rows:
            row['draft_qty'] = reserved.get(row['id'], 0)
            row['available_to_order'] = max(0, (row['qty'] or 0)-row['draft_qty'])
            review = self.reviews.get(row['id'])
            if review and review.get('fingerprint') == fingerprint(row, self.settings):
                row['review'] = review
            else:
                self.reviews.pop(row['id'], None)
                row['review'] = None

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        payload = dict(settings=self.settings, overrides=self.overrides, reviews=self.reviews,
                       orders=self.orders, anomaly_decisions=self.anomaly_decisions, automation=self.automation,
                       notifications=self.notifications[-50:], origin_signature=self.origin_signature)
        if self.custom_base:
            payload['dataset'] = self.base
        tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')), encoding='utf-8')
        tmp.replace(self.path)

    @contextmanager
    def transaction(self):
        keys = ('settings', 'overrides', 'reviews', 'orders', 'anomaly_decisions', 'automation', 'notifications')
        backup = {key: copy.deepcopy(getattr(self, key)) for key in keys}
        backup.update(base=self.base, custom_base=self.custom_base)
        try:
            yield
            self.recalculate()
            self.save()
        except Exception:
            for key, value in backup.items():
                setattr(self, key, value)
            self.recalculate()
            raise

    def notify(self, title, detail):
        self.notifications.append(dict(id=secrets.token_hex(8), at=purchase_orders.now(), title=title, detail=detail))
        self.notifications = self.notifications[-50:]

    def find_order(self, key):
        order = next((o for o in self.orders if o['id'] == key), None)
        if not order:
            raise ValueError('Заказ не найден')
        return order

    def create_orders(self, request):
        ids = request.get('ids')
        if not isinstance(ids, list) or not ids or len(ids) > 10000 or any(not isinstance(i, str) for i in ids):
            raise ValueError('Выберите товары для формирования заказа')
        lookup = {r['id']: r for r in self.rows}
        groups, skipped = defaultdict(list), []
        for key in dict.fromkeys(ids):
            row = lookup.get(key)
            if not row:
                raise ValueError('Неизвестный товар')
            wanted = (row.get('review') or {}).get('qty', row['qty'])
            qty = max(0, (wanted or 0)-row['draft_qty'])
            if not qty:
                skipped.append(dict(id=key, reason='Нет потребности или количество уже включено в черновик'))
                continue
            qty = math.ceil(max(qty, row['moq'])/row['pack']-1e-10)*row['pack']
            groups[row['supplier']].append(dict(row, create_qty=qty))
        if not groups:
            raise ValueError('Новых позиций нет: потребность уже покрыта или для расчёта нужен остаток.')
        result = []
        with self.transaction():
            for supplier, rows in groups.items():
                order = purchase_orders.new_order(supplier, rows, self.settings['as_of'], len(self.orders)+1, fingerprint, self.settings)
                self.orders.append(order)
                result.append(order)
            self.notify('Созданы черновики заказов', f'Поставщиков: {len(result)}. Проверьте количества и даты поступления.')
        return dict(orders=result, skipped=skipped)

    def update_order(self, request):
        order = self.find_order(request.get('id'))
        if order['status'] != 'draft':
            raise ValueError('Изменять можно только черновик заказа')
        expected = date.fromisoformat(str(request.get('expected_at', order['expected_at'])))
        if expected < date.fromisoformat(self.settings['as_of']) or expected > date.fromisoformat(self.settings['as_of'])+timedelta(days=365):
            raise ValueError('Дата поступления должна быть в пределах года после даты расчёта')
        changes = request.get('lines')
        if not isinstance(changes, list) or len(changes) != len(order['lines']):
            raise ValueError('Передайте все строки заказа; для исключения позиции укажите 0')
        lookup = {r['id']: r for r in self.rows}
        quantities = {}
        for line in changes:
            if not isinstance(line, dict) or line.get('item_id') in quantities:
                raise ValueError('Позиции заказа повторяются или имеют неверный формат')
            row = lookup.get(line.get('item_id'))
            if not row:
                raise ValueError('Товар отсутствует в текущем наборе. Восстановите исходные данные или отмените заказ.')
            quantities[row['id']] = purchase_orders.validate_quantity(line.get('qty'), row['pack'], row['moq'])
        if set(quantities) != {l['item_id'] for l in order['lines']} or not any(quantities.values()):
            raise ValueError('Состав заказа не совпадает или все количества равны 0. Для отмены используйте статус «Отменён».')
        with self.transaction():
            for line in order['lines']:
                row = lookup[line['item_id']]
                line.update(qty=quantities[line['item_id']], basis=fingerprint(row, self.settings),
                            recommended_qty=row['qty'], pack=row['pack'], moq=row['moq'], reason=row['reason'], warnings=row['warnings'])
            order.update(expected_at=expected.isoformat(), note=str(request.get('note', ''))[:1000], updated_at=purchase_orders.now())
            order['events'].append(dict(at=purchase_orders.now(), action='Черновик изменён; актуальный расчёт проверен'))
            order['warnings'] = list(dict.fromkeys(w for l in order['lines'] for w in l['warnings']))
        return order

    def order_status(self, request):
        order = self.find_order(request.get('id'))
        target = request.get('status')
        if request.get('confirmed') is not True:
            raise ValueError('Подтвердите изменение статуса заказа')
        if target not in {'draft': ('approved', 'cancelled'), 'approved': ('received', 'cancelled')}.get(order['status'], ()):
            raise ValueError('Недопустимый переход статуса. Повторное получение или размещение не выполняется.')
        lookup = {r['id']: r for r in self.rows}
        positive = [l for l in order['lines'] if l['qty'] > 0]
        if target in ('approved', 'received'):
            for line in positive:
                row = lookup.get(line['item_id'])
                if not row:
                    raise ValueError('Товар отсутствует в текущих данных. Сначала восстановите его в каталоге.')
                if target == 'approved':
                    if row['status'] != 'Черновик':
                        raise ValueError(f'{line["article"] or line["code"]}: подтвердите актуальный остаток и кратность в карточке товара.')
                    if fingerprint(row, self.settings) != line['basis']:
                        raise ValueError('Расчёт изменился после создания заказа. Проверьте количества и сохраните черновик заново.')
                    purchase_orders.validate_quantity(line['qty'], row['pack'], row['moq'])
                elif row['stock'] is None:
                    raise ValueError('Перед приёмкой внесите текущий остаток для каждой позиции.')
        if target == 'received' and request.get('stock_confirmed') is not True:
            raise ValueError('Подтвердите, что указанный остаток актуален и ещё не включает эту поставку.')
        with self.transaction():
            if target == 'received':
                for line in positive:
                    row = lookup[line['item_id']]
                    self.overrides.setdefault(row['id'], {}).update(stock=row['stock']+line['qty'], stock_date=self.settings['as_of'])
                order['received_as_of'] = self.settings['as_of']
            order.update(status=target, updated_at=purchase_orders.now())
            order['events'].append(dict(at=purchase_orders.now(), action='Статус: '+purchase_orders.STATUSES[target]))
            self.notify('Заказ '+order['number']+': '+purchase_orders.STATUSES[target], order['supplier'])
        return order

    def anomaly_decision(self, request):
        key, month, decision = request.get('id'), request.get('month'), request.get('decision')
        if decision not in ('auto', 'keep', 'exclude'):
            raise ValueError('Выберите автоматическую оценку, обычную или разовую продажу')
        row = next((r for r in self.rows if r['id'] == key), None)
        if not row or not any(h['month'] == month and h.get('candidate_removed', 0) > 0 for h in row['history']):
            raise ValueError('В этом месяце не найдено кандидата в разовый всплеск')
        with self.transaction():
            self.anomaly_decisions.setdefault(key, {})[month] = dict(decision=decision, note=str(request.get('note', ''))[:500], at=purchase_orders.now())
        return {'ok': True}

    def import_source(self, upload, request, kind):
        if kind == 'sales':
            dataset = refresh_sales(self.base, build_dataset(upload, request))
        else:
            dataset = merge_source(self.base, upload, request, kind)
        self.validate_order_catalog(dataset)
        with self.transaction():
            self.base, self.custom_base = dataset, True
            self.settings['as_of'] = dataset['as_of']
            replaces_stock = kind == 'stock' or (kind == 'sales' and 'stock' not in dataset.get('source_imports', {}) and request.get('mapping', {}).get('stock') is not None)
            if replaces_stock:
                # A new physical snapshot replaces manual receipt/stock adjustments.
                for value in self.overrides.values():
                    value.pop('stock', None)
                    value.pop('stock_date', None)
                if request.get('mapping', {}).get('pack') is not None:
                    for value in self.overrides.values():
                        value.pop('pack', None)
            self.notify('Данные обновлены', {'sales': 'История продаж', 'stock': 'Остатки', 'transit': 'Поставки в пути'}[kind]+': '+upload['file'])
        self.pending = None
        return {'ok': True, 'items': len(self.rows), 'kind': kind}

    def source_status(self):
        result = copy.deepcopy(self.base.get('source_imports', {}))
        for kind, label in [('sales', 'История продаж'), ('stock', 'Остатки'), ('transit', 'В пути')]:
            if kind in result:
                continue
            files = [s for s in self.base.get('sources', []) if s.get('kind', s.get('type')) == kind]
            present = (kind == 'sales' or any(i.get('stock') is not None for i in self.base['items'])) if kind != 'transit' else any(i.get('transit') for i in self.base['items'])
            if files or present:
                result[kind] = dict(kind=kind, file='Демонстрационные данные' if self.demo else ', '.join(str(s.get('file', s.get('name', label))) for s in files) or label+' в исходном наборе',
                                    row_count=None, as_of=None, warnings=['Исходный набор; даты остатков проверяются отдельно по товарам.'])
        return result

    def validate_order_catalog(self, dataset):
        known = {i['id'] for i in dataset['items']}
        if any(line['item_id'] not in known and line['qty'] > 0 for order in self.orders if order['status'] in ('draft', 'approved') for line in order['lines']):
            raise ValueError('В новой истории нет товаров из действующих заказов. Добавьте эти товары в файл или сначала отмените соответствующие заказы.')

    def configure_automation(self, request):
        from replenishment.automation import validate_config
        config = validate_config(request, self.automation)
        with self.transaction():
            if config['folder'] != self.automation['folder']:
                self.automation['hashes'] = {}
            self.automation.update(config)
            self.automation['next_run'] = (datetime.now()+timedelta(minutes=config['interval_minutes'])).isoformat(timespec='seconds') if config['enabled'] else None
        return {'ok': True, 'automation': self.automation}

    def run_automation(self):
        from replenishment.automation import collect_sources
        before = hashlib.sha256(json.dumps(self.rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        as_of = date.today().isoformat() if self.automation['advance_date'] else self.settings['as_of']
        try:
            changed, hashes = collect_sources(self.automation['folder'], self.automation.get('hashes', {}), as_of=as_of)
            if 'sales' in changed and self.mode != 'upload':
                raise ValueError('Сначала загрузите файл sales через кнопку «Загрузить продажи». Затем настройте папку в вашем наборе данных: тестовый пример и исходный кейс сохраняются отдельно.')
            dataset = self.base
            for kind in ('sales', 'stock', 'transit'):
                if kind not in changed:
                    continue
                source = changed[kind]
                dataset = refresh_sales(dataset, build_dataset(source['upload'], source['options'])) if kind == 'sales' else merge_source(dataset, source['upload'], source['options'], kind)
            self.validate_order_catalog(dataset)
            with self.transaction():
                if changed:
                    self.base, self.custom_base = dataset, True
                self.settings['as_of'] = as_of
                stock_source = changed.get('stock')
                if not stock_source and 'sales' in changed and 'stock' not in dataset.get('source_imports', {}) and changed['sales']['options']['mapping'].get('stock') is not None:
                    stock_source = changed['sales']
                if stock_source:
                    for value in self.overrides.values():
                        value.pop('stock', None)
                        value.pop('stock_date', None)
                        if stock_source['options']['mapping'].get('pack') is not None:
                            value.pop('pack', None)
                self.automation.update(last_run=purchase_orders.now(), last_error=None, hashes=hashes)
                self.automation['next_run'] = (datetime.now()+timedelta(minutes=self.automation['interval_minutes'])).isoformat(timespec='seconds') if self.automation['enabled'] else None
                self.recalculate()
                after = hashlib.sha256(json.dumps(self.rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                self.automation['changed'] = before != after
                if changed or before != after:
                    self.notify('Автообновление завершено', 'Обновлено источников: '+str(len(changed))+'. Расчёт потребности готов к проверке.')
        except (ValueError, OSError) as exc:
            with self.transaction():
                self.automation.update(last_run=purchase_orders.now(), last_error=str(exc), changed=False)
                self.automation['next_run'] = (datetime.now()+timedelta(minutes=self.automation['interval_minutes'])).isoformat(timespec='seconds') if self.automation['enabled'] else None
                self.notify('Не удалось обновить данные', str(exc))
            raise ValueError(str(exc)) from None
        return {'ok': True, 'automation': self.automation, 'changed_sources': list(changed)}

    def select(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        path = ROOT/'data/active-dataset.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'mode': self.mode}), encoding='utf-8')
        tmp.replace(path)

    def review(self, request):
        row = next((r for r in self.rows if r['id'] == request.get('id')), None)
        if not row:
            raise ValueError('Товар не найден')
        qty = request.get('qty')
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(qty) or qty < 0 or qty > 10**9:
            raise ValueError('Количество должно быть неотрицательным числом')
        if row['status'] != 'Черновик':
            raise ValueError('Сначала внесите актуальный остаток и кратность')
        if qty > 0 and (qty < row['moq'] or abs(qty/row['pack']-round(qty/row['pack'])) > 1e-7):
            raise ValueError('Количество не соответствует минимуму или кратности')
        if not request.get('confirmed'):
            raise ValueError('Подтвердите проверку количества и условий поставки')
        review = dict(qty=qty, note=str(request.get('note', ''))[:500],
                      at=datetime.now().isoformat(timespec='seconds'), fingerprint=fingerprint(row, self.settings))
        self.reviews[row['id']] = review
        row['review'] = review
        self.save()
        self.revision = secrets.token_hex(12)
        self.agent_cache = ToolCache()
        return review


class Handler(BaseHTTPRequestHandler):
    server_version = 'StockPilot/1.0'

    def send(self, data, status=200, content_type='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def check_host(self):
        return self.headers.get('Host', '').split(':')[0] in ('127.0.0.1', 'localhost')

    def do_GET(self):
        if not self.check_host():
            return self.send({'error': 'Недопустимый Host'}, 403)
        parsed = urlparse(self.path)
        state = self.server.state
        query = parse_qs(parsed.query)
        if parsed.path == '/api/health':
            return self.send(dict(service='StockPilot', version=2, products=len(state.rows),
                                  workspace=hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()))
        if parsed.path == '/api/state':
            with state.lock:
                rows = [{k: v for k, v in r.items() if k not in ('history', 'sources', 'anomalies', 'season', 'transit')} for r in state.rows]
                return self.send(dict(rows=rows, summary=summary(rows), settings=state.settings, csrf=self.server.csrf,
                                      mode=state.mode, agent='live' if os.environ.get('OPENAI_API_KEY') else 'demo',
                                      upload=state.dataset.get('upload'), available_upload=(ROOT/'data/uploaded-state.json').exists(),
                                      available_partner=(ROOT/'data/dataset.json').exists(),
                                      diagnostics=state.dataset.get('diagnostics'), limitations=state.dataset.get('limitations'),
                                      sources=state.dataset.get('sources'), categories=sorted(set(r['category'] for r in rows)),
                                      suppliers=sorted(set(r['supplier'] for r in rows)), source_imports=state.source_status(),
                                      orders_summary=purchase_orders.summary(state.orders), automation=state.automation, notifications=state.notifications[-15:], revision=state.revision))
        if parsed.path == '/api/import/template':
            try:
                return self.send(template_bytes(query.get('kind', ['sales'])[0]), content_type='text/csv; charset=utf-8')
            except ValueError as exc:
                return self.send({'error': str(exc)}, 400)
        if parsed.path == '/api/orders':
            with state.lock:
                return self.send({'orders': list(reversed(state.orders)), 'summary': purchase_orders.summary(state.orders)})
        if parsed.path == '/api/orders/item':
            with state.lock:
                try:
                    return self.send(state.find_order(query.get('id', [''])[0]))
                except ValueError as exc:
                    return self.send({'error': str(exc)}, 404)
        if parsed.path == '/api/anomalies':
            from replenishment.analytics import anomaly_report
            try:
                with state.lock:
                    return self.send(anomaly_report(state.dataset, state.settings, state.rows,
                        limit=int(query.get('limit', ['500'])[0]), offset=int(query.get('offset', ['0'])[0])))
            except (ValueError, TypeError) as exc:
                return self.send({'error': str(exc)}, 400)
        if parsed.path == '/api/item':
            key = parse_qs(parsed.query).get('id', [''])[0]
            with state.lock:
                row = next((r for r in state.rows if r['id'] == key), None)
                return self.send(row or {'error': 'Не найдено'}, 200 if row else 404)
        routes = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}
        if parsed.path in routes:
            file = ROOT/'static'/routes[parsed.path]
            return self.send(file.read_bytes(), content_type=mimetypes.guess_type(str(file))[0]+'; charset=utf-8')
        self.send({'error': 'Не найдено'}, 404)

    def do_POST(self):
        if not self.check_host() or self.headers.get('X-CSRF-Token') != self.server.csrf:
            return self.send({'error': 'Недопустимый запрос. Обновите страницу.'}, 403)
        try:
            length = int(self.headers.get('Content-Length', 0))
            limit = MAX_BYTES if self.path == '/api/import/preview' else 2_000_000
            if not 0 < length <= limit:
                raise ValueError('Недопустимый размер запроса')
            if self.path == '/api/import/preview':
                upload = inspect_file(self.rfile.read(length), unquote(self.headers.get('X-File-Name', '')))
                kind = self.headers.get('X-Import-Kind', 'sales')
                inspection = preview(upload, kind)
                token = secrets.token_urlsafe(24)
                with self.server.state.lock:
                    self.server.state.pending = (token, upload, kind)
                return self.send(dict(inspection, token=token))
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError('Нужен JSON-объект')
            state = self.server.state
            if self.path == '/api/agent':
                with state.lock:
                    revision = state.revision
                    if request.get('revision') not in (None, revision):
                        return self.send({'error': 'Расчёт обновился. Повторите вопрос по актуальным данным.'}, 409)
                    dataset, settings, rows = state.dataset, copy.deepcopy(state.settings), state.rows
                    tool_cache = state.agent_cache
                response = ask(str(request.get('message', '')), dataset, settings, rows, request.get('history'), request.get('context'), tool_cache)
                with state.lock:
                    if self.server.state is not state or revision != state.revision:
                        return self.send({'error': 'Данные изменились во время ответа. Задайте вопрос заново.'}, 409)
                    response['revision'] = revision
                    return self.send(response)
            if self.path == '/api/backtest':
                from replenishment.analytics import backtest
                months = request.get('months', 3)
                if type(months) is not int or not 1 <= months <= 3:
                    raise ValueError('Выберите от 1 до 3 месяцев проверки')
                with state.lock:
                    dataset, settings = copy.deepcopy(state.dataset), copy.deepcopy(state.settings)
                return self.send(backtest(dataset, settings, months=months))
            with state.lock:
                if self.path == '/api/import/commit':
                    if not state.pending or request.get('token') != state.pending[0]:
                        raise ValueError('Предпросмотр устарел. Выберите файл заново.')
                    kind = request.get('kind', state.pending[2])
                    if kind != state.pending[2]:
                        raise ValueError('Тип источника изменился. Выберите файл заново.')
                    if kind == 'sales' and state.mode != 'upload':
                        if (ROOT/'data/uploaded-state.json').exists():
                            replacement = State(mode='upload')
                            replacement.import_source(state.pending[1], request, kind)
                        else:
                            dataset = build_dataset(state.pending[1], request)
                            replacement = State(mode='upload', dataset=dataset)
                            replacement.save()
                        replacement.select()
                        self.server.state = replacement
                        return self.send({'ok': True, 'items': len(replacement.rows), 'kind': kind})
                    return self.send(state.import_source(state.pending[1], request, kind))
                if self.path == '/api/dataset':
                    if request.get('mode') not in ('partner', 'demo', 'upload'):
                        raise ValueError('Неизвестный набор данных')
                    replacement = State(mode=request['mode'])
                    replacement.select()
                    self.server.state = replacement
                    return self.send({'ok': True})
                if self.path == '/api/calculate':
                    with state.transaction():
                        state.settings = validate_settings({**state.settings, **request})
                    return self.send({'ok': True})
                if self.path == '/api/orders/create':
                    return self.send(state.create_orders(request))
                if self.path == '/api/orders/update':
                    return self.send(state.update_order(request))
                if self.path == '/api/orders/status':
                    return self.send(state.order_status(request))
                if self.path == '/api/orders/export':
                    data, mime = purchase_orders.export_order(state.find_order(request.get('id')), request.get('format', 'xlsx'))
                    return self.send(data, content_type=mime)
                if self.path == '/api/anomalies/decision':
                    return self.send(state.anomaly_decision(request))
                if self.path == '/api/automation':
                    return self.send(state.configure_automation(request))
                if self.path == '/api/automation/run':
                    return self.send(state.run_automation())
                if self.path == '/api/review':
                    return self.send(state.review(request))
                if self.path == '/api/override':
                    key = request.get('id')
                    if key not in {r['id'] for r in state.rows}:
                        raise ValueError('Неизвестный код товара')
                    stock, pack = request.get('stock'), request.get('pack')
                    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in [stock, pack]):
                        raise ValueError('Остаток и кратность должны быть числами')
                    if stock < 0 or pack <= 0 or stock > 10**9 or pack > 10**6:
                        raise ValueError('Неверный остаток или кратность')
                    if not request.get('confirmed'):
                        raise ValueError('Подтвердите дату остатка')
                    with state.transaction():
                        state.overrides[key] = dict(stock=stock, stock_date=state.settings['as_of'], pack=pack)
                    return self.send({'ok': True})
                if self.path == '/api/export':
                    ids = request.get('ids', [])
                    if not isinstance(ids, list) or len(ids) > 10000:
                        raise ValueError('Неверный список позиций')
                    rows = [r for r in state.rows if r['id'] in set(ids)]
                    rows.sort(key=lambda r: (r['supplier'], r['article']))
                    return self.send(csv_bytes(rows), content_type='text/csv; charset=utf-8')
            self.send({'error': 'Не найдено'}, 404)
        except (ValueError, TypeError, KeyError) as exc:
            self.send({'error': str(exc)}, 400)
        except RuntimeError as exc:
            self.send({'error': str(exc)}, 502)
        except Exception:
            self.send({'error': 'Внутренняя ошибка. Проверьте формат данных.'}, 500)
            import traceback
            traceback.print_exc()

    def log_message(self, format, *args):
        if args and str(args[0]).startswith('POST'):
            super().log_message(format, *args)


def start_scheduler(server):
    """Refresh only the active local workspace while this application is running."""
    stop = threading.Event()
    def tick():
        while not stop.wait(5):
            state = server.state
            try:
                with state.lock:
                    config = state.automation
                    if server.state is not state or not config['enabled']:
                        continue
                    next_run = config.get('next_run')
                    if not next_run or datetime.fromisoformat(next_run) <= datetime.now():
                        state.run_automation()
            except Exception as exc:
                # An invalid file is already reported in the application's status.
                # Unexpected errors must not stop future refreshes or print inputs.
                if not isinstance(exc, ValueError):
                    with state.lock:
                        state.automation.update(last_error='Ошибка автообновления. Проверьте настройки папки.',
                            next_run=(datetime.now()+timedelta(minutes=state.automation['interval_minutes'])).isoformat(timespec='seconds'))
    threading.Thread(target=tick, name='stockpilot-refresh', daemon=True).start()
    return stop


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    load_env()
    if not args.demo and not (ROOT/'data/dataset.json').exists() and not (ROOT/'data/active-dataset.json').exists():
        print('Partner data not imported. Starting with synthetic demo data.')
        args.demo = True
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.state = State(demo=args.demo)
    server.csrf = secrets.token_urlsafe(32)
    stop_scheduler = start_scheduler(server)
    print(f'StockPilot: http://127.0.0.1:{args.port} | {len(server.state.rows)} products', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_scheduler.set()
        server.server_close()


if __name__ == '__main__':
    main()
