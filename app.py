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
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from replenishment.agent import ask, summary
from replenishment.demo import make_demo
from replenishment.engine import calculate_all, number

ROOT = Path(__file__).resolve().parent
DEFAULTS = dict(as_of='2026-09-22', lead_days=30, review_days=14, safety_days=7, growth_pct=0,
                seasonality=True, trend=True, remove_outliers=True, compensate_stockouts=True, category_policies={})


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
    return result


def fingerprint(row, settings):
    payload = {k: v for k, v in row.items() if k not in ['review', 'sources']}
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


class State:
    def __init__(self, demo=False):
        self.lock = threading.RLock()
        self.demo = demo
        self.path = ROOT/'data'/('demo-state.json' if demo else 'local-state.json')
        self.base = make_demo() if demo else json.loads((ROOT/'data/dataset.json').read_text(encoding='utf-8'))
        self.settings = copy.deepcopy(DEFAULTS)
        self.overrides, self.reviews = {}, {}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            self.settings = validate_settings(saved.get('settings', {}))
            self.overrides = saved.get('overrides', {})
            self.reviews = saved.get('reviews', {})
        self.recalculate()

    def recalculate(self):
        self.dataset = copy.deepcopy(self.base)
        for item in self.dataset['items']:
            item.update(self.overrides.get(item['id'], {}))
        self.rows = calculate_all(self.dataset, self.settings)
        for row in self.rows:
            review = self.reviews.get(row['id'])
            if review and review.get('fingerprint') == fingerprint(row, self.settings):
                row['review'] = review
            else:
                self.reviews.pop(row['id'], None)
                row['review'] = None

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(dict(settings=self.settings, overrides=self.overrides, reviews=self.reviews), ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.path)

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
        if parsed.path == '/api/health':
            return self.send(dict(service='StockPilot', version=1, products=len(state.rows)))
        if parsed.path == '/api/state':
            with state.lock:
                rows = [{k: v for k, v in r.items() if k not in ('history', 'sources', 'anomalies', 'season', 'transit')} for r in state.rows]
                return self.send(dict(rows=rows, summary=summary(rows), settings=state.settings, csrf=self.server.csrf,
                                      mode='demo' if state.demo else 'partner', agent='live' if os.environ.get('OPENAI_API_KEY') else 'demo',
                                      diagnostics=state.dataset.get('diagnostics'), limitations=state.dataset.get('limitations'),
                                      sources=state.dataset.get('sources'), categories=sorted(set(r['category'] for r in rows))))
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
            if not 0 < length <= 100_000:
                raise ValueError('Недопустимый размер запроса')
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError('Нужен JSON-объект')
            state = self.server.state
            if self.path == '/api/agent':
                with state.lock:
                    dataset, settings, rows = state.dataset, copy.deepcopy(state.settings), state.rows
                return self.send(ask(str(request.get('message', '')), dataset, settings, rows, request.get('history')))
            with state.lock:
                if self.path == '/api/calculate':
                    state.settings = validate_settings(request)
                    state.recalculate()
                    state.save()
                    return self.send({'ok': True})
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
                    state.overrides[key] = dict(stock=stock, stock_date=state.settings['as_of'], pack=pack)
                    state.recalculate()
                    state.save()
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    load_env()
    if not args.demo and not (ROOT/'data/dataset.json').exists():
        print('Partner data not imported. Starting with synthetic demo data.')
        args.demo = True
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.state = State(demo=args.demo)
    server.csrf = secrets.token_urlsafe(32)
    print(f'StockPilot: http://127.0.0.1:{args.port} | {len(server.state.rows)} products', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
