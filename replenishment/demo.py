"""Small synthetic dataset for a public demonstration, independent of partner data."""
from datetime import date, timedelta
from .engine import clean_transactions


def make_demo():
    items = []
    for n, name in enumerate(['Автоматический выключатель 16А', 'Розетка с заземлением', 'Кабель силовой',
                              'Светильник потолочный', 'Рамка на 2 поста', 'Контактор 25А']):
        supplier = 'IEK' if n % 2 == 0 else 'Systeme Electric'
        sales = {}
        for y in (2024, 2025, 2026):
            for m in range(1, 13):
                if (y, m) > (2026, 8):
                    break
                sales[f'{y}-{m:02}'] = round((50+15*n)*(1+(y-2024)*.08)*(1.5 if m in (6, 7, 8) else 1))
        obj = dict(id=f'{supplier}:DEMO{n+1:03}', code=f'DEMO{n+1:03}', article=f'DEMO-{n+1:03}',
                   name=name+' [синтетические данные]', supplier=supplier, unit='м' if n == 2 else 'шт',
                   category='A' if n < 3 else 'B', sales=sales, stock=20+n*5, stock_date='2026-09-22',
                   pack=10, moq=20, sources=['Встроенный синтетический набор'], warnings=[],
                   transit=[dict(qty=30, eta='2026-10-05', source='Демо-поставка')])
        if n == 0:
            obj['sales']['2026-07'] = 5000
            obj['outlier_removed'] = {'2026-07': 4913}
            obj['anomalies'] = [dict(date='2026-07-12', qty=4923, regular=10, reason='Синтетический разовый проект')]
        if n == 1:
            obj['sales']['2026-08'] = 50
            obj['stockout_days'] = {'2026-08': 16}
        items.append(obj)
    return dict(version=1, as_of='2026-09-22', items=items, seasonality={
        'IEK': [.8, .8, .9, 1, 1, 1.4, 1.5, 1.4, 1.1, 1, .8, .8],
        'Systeme Electric': [.8, .8, .9, 1, 1, 1.4, 1.5, 1.4, 1.1, 1, .8, .8]},
        diagnostics={'synthetic_items': len(items)}, sources=[],
        limitations=['Это синтетический набор для демонстрации. Он не представляет реальные заказы.'])
