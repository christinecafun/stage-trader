"""
All Dash callbacks. Registered via register(app, store, worker).
"""
import json

import plotly.graph_objects as go
import dash_bootstrap_components as dbc
import dash
from dash import Input, Output, State, callback_context, html, dcc, no_update

import config
from analysis.stages import STAGE_COLORS, STAGE_LABELS, recommend_position_size


def register(app, store, worker):

    # ==================================================================
    # Interval → refresh shared outputs
    # ==================================================================
    @app.callback(
        Output('conn-badge', 'children'),
        Output('conn-badge', 'style'),
        Output('last-update', 'children'),
        Output('error-alert', 'children'),
        Output('error-alert', 'is_open'),
        Output('stat-netliq', 'children'),
        Output('stat-cash', 'children'),
        Output('stat-upnl', 'children'),
        Output('stat-dpnl', 'children'),
        Output('goal-bar', 'value'),
        Output('goal-label', 'children'),
        Input('interval', 'n_intervals'),
    )
    def refresh_header(n):
        data = store.get_snapshot()

        # Connection badge
        if data['connected']:
            badge = '●  Connected'
            badge_style = {'backgroundColor': '#28a745'}
        else:
            badge = '●  Disconnected'
            badge_style = {'backgroundColor': '#dc3545'}

        last = f"Last update: {data['last_update']}" if data['last_update'] else ''
        error_msg = data['error'] or ''
        error_open = bool(error_msg)

        # Account metrics
        acc = data['account']
        netliq = _fmt_usd(acc.get('NetLiquidation'))
        cash = _fmt_usd(acc.get('TotalCashValue'))
        upnl = _fmt_usd_signed(acc.get('UnrealizedPnL'))
        dpnl = _fmt_usd_signed(acc.get('RealizedPnL'))

        # Goal progress
        try:
            nlv = float(acc.get('NetLiquidation', 0))
            pct = min(100, (nlv / config.GOAL_AMOUNT) * 100)
            goal_label = f'${nlv:,.2f} of ${config.GOAL_AMOUNT:,} goal  ({pct:.1f}%)'
        except Exception:
            pct = 0
            goal_label = f'Goal: ${config.GOAL_AMOUNT:,}'

        return (badge, badge_style, last, error_msg, error_open,
                netliq, cash, upnl, dpnl, pct, goal_label)

    # ==================================================================
    # Tabs
    # ==================================================================
    @app.callback(
        Output('tab-content', 'children'),
        Input('tabs', 'active_tab'),
        Input('interval', 'n_intervals'),
    )
    def render_tab(active_tab, n):
        data = store.get_snapshot()
        if active_tab == 'portfolio':
            return _portfolio_tab(data)
        elif active_tab == 'analysis':
            return _analysis_tab(data)
        elif active_tab == 'execute':
            return _execute_tab(data)
        elif active_tab == 'history':
            return _history_tab(data)
        return html.Div()

    # ==================================================================
    # Preview order → open modal
    # ==================================================================
    @app.callback(
        Output('order-modal', 'is_open'),
        Output('order-modal-body', 'children', allow_duplicate=True),
        Output('pending-order', 'data', allow_duplicate=True),
        Input('preview-order-btn', 'n_clicks'),
        State('ex-symbol', 'value'),
        State('ex-action', 'value'),
        State('ex-usd', 'value'),
        State('ex-tp-pct', 'value'),
        State('ex-sl-pct', 'value'),
        State('ex-qty-override', 'value'),
        prevent_initial_call=True,
    )
    def preview_order(n_clicks, symbol, action, usd_str, tp_pct, sl_pct, qty_override):
        if not symbol or not action:
            return no_update, no_update, no_update

        symbol = symbol.upper().strip()
        data = store.get_snapshot()

        # Determine price
        pos_match = next((p for p in data['positions'] if p['symbol'] == symbol), None)
        sd = data['stage_data'].get(symbol, {})
        price = sd.get('price') or (pos_match['current_price'] if pos_match else None)

        if not price:
            return True, dbc.Alert('Could not determine current price. Is TWS connected?',
                                   color='warning'), no_update

        tp = float(tp_pct or config.PROFIT_TARGET_PCT * 100) / 100
        sl = float(sl_pct or config.STOP_LOSS_PCT * 100) / 100

        if action == 'BUY':
            # Use risk-based sizing unless user typed an explicit amount
            account_value, settled_cash = _account_figures(data['account'])
            if usd_str:
                usd_amount = float(usd_str)
                sizing_reason = None
            else:
                sizing = recommend_position_size(
                    entry=price, stop=sd.get('stop'),
                    account_value=account_value, settled_cash=settled_cash,
                )
                usd_amount = sizing['usd'] if sizing['usd'] > 0 else config.DEFAULT_TRADE_USD
                sizing_reason = sizing['reason']

            qty = float(qty_override) if qty_override else round(usd_amount / price, 4)
            tp_price = round(price * (1 + tp), 2)
            sl_price = round(price * (1 - sl), 2)
            total_usd = round(qty * price, 2)

            params = {
                'action': 'BUY',
                'symbol': symbol,
                'usd_amount': total_usd,
                'limit_price': price,
                'profit_target_pct': tp,
                'stop_loss_pct': sl,
                'use_fractional': config.USE_FRACTIONAL,
            }

            body = _order_preview_body(
                action='BUY', symbol=symbol, qty=qty, price=price,
                tp=tp_price, sl=sl_price, usd=total_usd,
                tp_pct=tp * 100, sl_pct=sl * 100,
                stage_label=sd.get('label', ''),
                sizing_reason=sizing_reason,
            )

        else:  # SELL
            position = pos_match['position'] if pos_match else 0
            qty = float(qty_override) if qty_override else abs(position)
            params = {
                'action': 'SELL',
                'symbol': symbol,
                'quantity': qty,
            }
            body = _order_preview_body(
                action='SELL', symbol=symbol, qty=qty, price=price,
                tp=None, sl=None, usd=round(qty * price, 2),
                tp_pct=None, sl_pct=None,
                stage_label=sd.get('label', ''),
                sizing_reason=None,
            )

        return True, body, params

    # ==================================================================
    # Live preview update as user types USD amount
    # ==================================================================
    @app.callback(
        Output('order-modal-body', 'children', allow_duplicate=True),
        Input('modal-usd-input', 'value'),
        State('pending-order', 'data'),
        prevent_initial_call=True,
    )
    def recalculate_order(usd_val, pending):
        if not pending or not usd_val:
            return no_update
        try:
            usd = float(usd_val)
        except (TypeError, ValueError):
            return no_update
        if usd <= 0 or pending.get('action') != 'BUY':
            return no_update

        price = pending['limit_price']
        tp    = pending['profit_target_pct']
        sl    = pending['stop_loss_pct']
        qty      = round(usd / price, 4)
        tp_price = round(price * (1 + tp), 2)
        sl_price = round(price * (1 - sl), 2)

        sd = store.get_snapshot()['stage_data'].get(pending['symbol'], {})
        return _order_preview_body(
            action='BUY', symbol=pending['symbol'], qty=qty, price=price,
            tp=tp_price, sl=sl_price, usd=round(qty * price, 2),
            tp_pct=tp * 100, sl_pct=sl * 100,
            stage_label=sd.get('label', ''),
        )

    # ==================================================================
    # Execute confirmed order
    # ==================================================================
    @app.callback(
        Output('order-modal', 'is_open', allow_duplicate=True),
        Output('execute-result-alert', 'children'),
        Output('execute-result-alert', 'is_open'),
        Output('execute-result-alert', 'color'),
        Input('execute-btn', 'n_clicks'),
        Input('cancel-btn', 'n_clicks'),
        State('pending-order', 'data'),
        State('modal-usd-input', 'value'),
        prevent_initial_call=True,
    )
    def execute_or_cancel(exec_clicks, cancel_clicks, pending, modal_usd):
        ctx = callback_context
        if not ctx.triggered:
            return no_update, no_update, no_update, no_update

        if 'cancel-btn' in ctx.triggered[0]['prop_id']:
            return False, no_update, no_update, no_update

        if not pending:
            return False, 'No order pending.', True, 'warning'

        # Always use the current USD input — what you see is what gets sent
        if pending.get('action') == 'BUY' and modal_usd:
            try:
                usd = float(modal_usd)
                if usd > 0:
                    price = pending['limit_price']
                    qty   = round(usd / price, 4)
                    pending = {**pending, 'usd_amount': round(qty * price, 2)}
            except (TypeError, ValueError):
                pass

        order_id = worker.request_order(pending)
        msg = f"Order submitted (id: {order_id}). Check Order History tab for status."
        return False, msg, True, 'success'

    # ==================================================================
    # Recommendation row buttons → open modal with sized position
    # ==================================================================
    @app.callback(
        Output('order-modal', 'is_open', allow_duplicate=True),
        Output('order-modal-body', 'children', allow_duplicate=True),
        Output('pending-order', 'data', allow_duplicate=True),
        Output('modal-usd-input', 'value'),
        Input({'type': 'rec-btn', 'index': dash.ALL}, 'n_clicks'),
        prevent_initial_call=True,
    )
    def rec_button_clicked(clicks):
        ctx = callback_context
        if not ctx.triggered or not any(c for c in clicks if c):
            return no_update, no_update, no_update, no_update

        tid = ctx.triggered_id
        if not tid:
            return no_update, no_update, no_update, no_update

        parts = tid['index'].split('|')
        symbol = parts[0]
        action = parts[1] if len(parts) > 1 else 'BUY'

        data = store.get_snapshot()
        pos_match = next((p for p in data['positions'] if p['symbol'] == symbol), None)
        sd = data['stage_data'].get(symbol, {})
        price = sd.get('price') or (pos_match['current_price'] if pos_match else None)

        if not price:
            return True, dbc.Alert('Could not get current price.', color='warning'), no_update, no_update

        if action == 'BUY':
            tp = config.PROFIT_TARGET_PCT
            sl = config.STOP_LOSS_PCT

            # Size position using 2% risk rule against settled cash
            account_value, settled_cash = _account_figures(data['account'])
            sizing = recommend_position_size(
                entry=price,
                stop=sd.get('stop'),
                account_value=account_value,
                settled_cash=settled_cash,
            )
            usd = sizing['usd'] if sizing['usd'] > 0 else config.DEFAULT_TRADE_USD

            qty = round(usd / price, 4)
            tp_price = round(price * (1 + tp), 2)
            sl_price = round(price * (1 - sl), 2)
            params = {
                'action': 'BUY',
                'symbol': symbol,
                'usd_amount': round(qty * price, 2),
                'limit_price': price,
                'profit_target_pct': tp,
                'stop_loss_pct': sl,
                'use_fractional': config.USE_FRACTIONAL,
            }
            body = _order_preview_body(
                action='BUY', symbol=symbol, qty=qty, price=price,
                tp=tp_price, sl=sl_price, usd=round(qty * price, 2),
                tp_pct=tp * 100, sl_pct=sl * 100,
                stage_label=sd.get('label', ''),
                sizing_reason=sizing['reason'],
            )
        else:
            qty = abs(pos_match['position']) if pos_match else 0
            usd = round(qty * price, 2)
            params = {'action': 'SELL', 'symbol': symbol, 'quantity': qty}
            body = _order_preview_body(
                action='SELL', symbol=symbol, qty=qty, price=price,
                tp=None, sl=None, usd=usd,
                tp_pct=None, sl_pct=None,
                stage_label=sd.get('label', ''),
                sizing_reason=None,
            )

        return True, body, params, usd


# ===========================================================================
# Tab builders
# ===========================================================================

def _portfolio_tab(data):
    positions = data['positions']
    stage_data = data['stage_data']

    if not positions:
        return dbc.Alert('No open positions found. Make sure TWS is connected.', color='info')

    rows = []
    for p in positions:
        sym = p['symbol']
        sd = stage_data.get(sym, {})
        stage = sd.get('stage', 0)
        color = STAGE_COLORS.get(stage, '#6c757d')
        label = sd.get('label', 'Unknown')

        pnl = p['unrealized_pnl']
        pnl_color = '#28a745' if pnl >= 0 else '#dc3545'

        rows.append(html.Tr([
            html.Td(html.Strong(sym)),
            html.Td(f"{p['position']:,.4f}"),
            html.Td(f"${p['avg_cost']:.4f}"),
            html.Td(f"${p['current_price']:.4f}"),
            html.Td(f"${p['market_value']:,.2f}"),
            html.Td(html.Span(f"${pnl:+,.2f} ({p['unrealized_pnl_pct']:+.1f}%)",
                              style={'color': pnl_color})),
            html.Td(html.Span(label, className='badge rounded-pill',
                              style={'backgroundColor': color, 'fontSize': '0.75rem'})),
        ]))

    table = dbc.Table(
        [html.Thead(html.Tr([
            html.Th('Symbol'), html.Th('Shares'), html.Th('Avg Cost'),
            html.Th('Price'), html.Th('Mkt Value'), html.Th('Unrealised P&L'),
            html.Th('Stage'),
        ])),
         html.Tbody(rows)],
        striped=True, hover=True, responsive=True, className='mb-0',
    )
    return table


def _analysis_tab(data):
    positions = data['positions']
    stage_data = data['stage_data']
    chart_bars = data['chart_bars']

    if not positions:
        return dbc.Alert('No positions to analyse.', color='info')

    cards = []
    for p in positions:
        sym = p['symbol']
        sd = stage_data.get(sym, {})
        bars = chart_bars.get(sym, [])
        stage = sd.get('stage', 0)
        color = STAGE_COLORS.get(stage, '#6c757d')

        fig = _build_chart(sym, bars, sd)

        info_rows = [
            ('MA 150-day', f"${sd['ma150']:.2f}" if sd.get('ma150') else '—'),
            ('MA 50-day', f"${sd['ma50']:.2f}" if sd.get('ma50') else '—'),
            ('MA Slope', f"{sd['ma150_slope']:+.2f}%" if sd.get('ma150_slope') is not None else '—'),
            ('Vol Expanding', 'Yes ✓' if sd.get('vol_expanding') else 'No'),
            ('RS vs SPY', f"{sd['rs']:.0f}" if sd.get('rs') is not None else '—'),
            ('52-wk High', f"${sd['high_52w']:.2f}" if sd.get('high_52w') else '—'),
            ('52-wk Low', f"${sd['low_52w']:.2f}" if sd.get('low_52w') else '—'),
            ('Measured Target', f"${sd['measured_target']:.2f}" if sd.get('measured_target') else '—'),
            ('Suggested Stop', f"${sd['stop']:.2f}" if sd.get('stop') else '—'),
        ]

        card = dbc.Card(className='mb-3', style={'backgroundColor': '#1e1e2e', 'border': f'1px solid {color}'}, children=[
            dbc.CardHeader(children=[
                dbc.Row(align='center', children=[
                    dbc.Col(html.Strong(sym, className='fs-5 text-white'), width='auto'),
                    dbc.Col(
                        html.Span(sd.get('label', 'Unknown'), className='badge fs-6',
                                  style={'backgroundColor': color}),
                        width='auto',
                    ),
                    dbc.Col(
                        html.Small(f"Action: {sd.get('action', '—')}", className='text-secondary'),
                        width='auto',
                    ),
                ]),
            ]),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col(dcc.Graph(figure=fig, config={'displayModeBar': False},
                                     style={'height': '280px'}), md=8),
                    dbc.Col([
                        dbc.Table(
                            [html.Tbody([
                                html.Tr([html.Td(k, className='text-secondary pe-3'),
                                         html.Td(v, className='text-white fw-bold')])
                                for k, v in info_rows
                            ])],
                            size='sm', borderless=True, className='mb-0',
                        ),
                    ], md=4),
                ]),
            ]),
        ])
        cards.append(card)

    return html.Div(cards)


def _execute_tab(data):
    positions = data['positions']
    recs = data['recommendations']

    # Recommendations section
    rec_rows = []
    for r in recs:
        color = r.get('color', '#6c757d')
        action = r['action']
        btn_color = 'danger' if 'SELL' in action else ('warning' if 'REDUCE' in action else 'success')

        rec_rows.append(html.Tr([
            html.Td(html.Strong(r['symbol'])),
            html.Td(html.Span(r['label'], className='badge', style={'backgroundColor': color})),
            html.Td(r['reason'], className='text-secondary', style={'fontSize': '0.85rem'}),
            html.Td(f"${r['current_price']:.2f}"),
            html.Td(f"${r['target']:.2f}" if r.get('target') else '—'),
            html.Td(f"${r['stop']:.2f}" if r.get('stop') else '—'),
            html.Td(
                dbc.Button(action, size='sm', color=btn_color,
                           id={'type': 'rec-btn',
                               'index': f"{r['symbol']}|{'SELL' if 'SELL' in action or 'REDUCE' in action else 'BUY'}"}),
            ),
        ]))

    rec_table = dbc.Table(
        [html.Thead(html.Tr([
            html.Th('Symbol'), html.Th('Stage'), html.Th('Reason'),
            html.Th('Price'), html.Th('Target'), html.Th('Stop'), html.Th(''),
        ])),
         html.Tbody(rec_rows if rec_rows else [html.Tr([html.Td('No recommendations yet.', colSpan=7)])])],
        striped=True, hover=True, responsive=True, size='sm',
    )

    # Manual trade form
    symbol_options = [{'label': p['symbol'], 'value': p['symbol']} for p in positions]

    form = dbc.Card(className='mt-4', style={'backgroundColor': '#1e1e2e'}, children=[
        dbc.CardHeader(html.Strong('Manual Order Entry', className='text-white')),
        dbc.CardBody([
            dbc.Row(className='g-3', children=[
                dbc.Col(md=2, children=[
                    dbc.Label('Symbol', className='text-secondary'),
                    dbc.Input(id='ex-symbol', placeholder='AAPL', type='text',
                              style={'backgroundColor': '#2a2a3e', 'color': 'white', 'border': '1px solid #444'}),
                ]),
                dbc.Col(md=2, children=[
                    dbc.Label('Action', className='text-secondary'),
                    dbc.RadioItems(
                        id='ex-action',
                        options=[{'label': 'BUY', 'value': 'BUY'}, {'label': 'SELL', 'value': 'SELL'}],
                        value='BUY',
                        inline=True,
                        inputCheckedClassName='border-success',
                    ),
                ]),
                dbc.Col(md=2, children=[
                    dbc.Label('USD Amount ($)', className='text-secondary'),
                    dbc.Input(id='ex-usd', type='number', min=1, step=50,
                              value=config.DEFAULT_TRADE_USD,
                              style={'backgroundColor': '#2a2a3e', 'color': 'white', 'border': '1px solid #444'}),
                ]),
                dbc.Col(md=2, children=[
                    dbc.Label('Profit Target (%)', className='text-secondary'),
                    dbc.Input(id='ex-tp-pct', type='number', min=1, max=200, step=1,
                              value=round(config.PROFIT_TARGET_PCT * 100),
                              style={'backgroundColor': '#2a2a3e', 'color': 'white', 'border': '1px solid #444'}),
                ]),
                dbc.Col(md=2, children=[
                    dbc.Label('Stop Loss (%)', className='text-secondary'),
                    dbc.Input(id='ex-sl-pct', type='number', min=1, max=50, step=1,
                              value=round(config.STOP_LOSS_PCT * 100),
                              style={'backgroundColor': '#2a2a3e', 'color': 'white', 'border': '1px solid #444'}),
                ]),
                dbc.Col(md=2, children=[
                    dbc.Label('Shares Override', className='text-secondary'),
                    dbc.Input(id='ex-qty-override', type='number', min=0, step=0.0001,
                              placeholder='auto',
                              style={'backgroundColor': '#2a2a3e', 'color': 'white', 'border': '1px solid #444'}),
                ]),
            ]),
            dbc.Row(className='mt-3', children=[
                dbc.Col(width='auto', children=[
                    dbc.Button('Preview Order', id='preview-order-btn', color='primary'),
                ]),
                dbc.Col(children=[
                    dbc.Alert(id='execute-result-alert', is_open=False, dismissable=True,
                              className='mb-0 py-2'),
                ]),
            ]),
        ]),
    ])

    return html.Div([
        html.H6('Recommendations', className='text-secondary mb-2'),
        rec_table,
        form,
    ])


def _history_tab(data):
    orders = data['recent_orders']
    if not orders:
        return dbc.Alert('No orders placed this session.', color='info')

    rows = []
    for o in orders:
        our_status = o.get('status', '—')
        ibkr_status = o.get('ibkr_status', '')
        error = o.get('error', '')

        if our_status == 'error':
            badge = html.Span('FAILED', className='badge bg-danger')
        elif ibkr_status in ('Submitted', 'PreSubmitted', 'Filled'):
            badge = html.Span(ibkr_status.upper(), className='badge bg-success')
        elif ibkr_status in ('Inactive', 'ApiCancelled', 'Cancelled'):
            badge = html.Span(ibkr_status.upper(), className='badge bg-danger')
        else:
            badge = html.Span(ibkr_status.upper() or 'SENT', className='badge bg-secondary')

        note = error or ''

        rows.append(html.Tr([
            html.Td(o.get('ts', '—')),
            html.Td(o.get('symbol', '—')),
            html.Td(o.get('action', '—')),
            html.Td(f"{o.get('quantity', '—')}"),
            html.Td(f"${o['entry']:.2f}" if o.get('entry') else '—'),
            html.Td(f"${o['take_profit']:.2f}" if o.get('take_profit') else '—'),
            html.Td(f"${o['stop_loss']:.2f}" if o.get('stop_loss') else '—'),
            html.Td(badge),
            html.Td(note, className='text-danger', style={'fontSize': '0.8rem'}),
        ]))

    return dbc.Table(
        [html.Thead(html.Tr([
            html.Th('Time'), html.Th('Symbol'), html.Th('Action'), html.Th('Qty'),
            html.Th('Entry'), html.Th('TP'), html.Th('SL'), html.Th('Status'), html.Th('Note'),
        ])),
         html.Tbody(rows)],
        striped=True, hover=True, responsive=True,
    )


# ===========================================================================
# Chart builder
# ===========================================================================

def _build_chart(symbol, bars, sd):
    fig = go.Figure()

    if not bars:
        fig.update_layout(_chart_layout(symbol, empty=True))
        return fig

    dates = [b['date'] for b in bars]
    closes = [b['close'] for b in bars]
    opens = [b['open'] for b in bars]
    highs = [b['high'] for b in bars]
    lows = [b['low'] for b in bars]
    volumes = [b['volume'] for b in bars]

    # Candlestick
    fig.add_trace(go.Candlestick(
        x=dates, open=opens, high=highs, low=lows, close=closes,
        name=symbol, increasing_line_color='#28a745', decreasing_line_color='#dc3545',
        showlegend=False,
    ))

    # MA lines — compute manually from bars list
    import pandas as pd
    s = pd.Series(closes)
    ma150 = s.rolling(150, min_periods=80).mean().tolist()
    ma50 = s.rolling(50, min_periods=30).mean().tolist()

    fig.add_trace(go.Scatter(
        x=dates, y=ma150, name='MA 150', line=dict(color='#f7c59f', width=1.5, dash='dot'),
        showlegend=True,
    ))
    fig.add_trace(go.Scatter(
        x=dates, y=ma50, name='MA 50', line=dict(color='#87ceeb', width=1),
        showlegend=True,
    ))

    # Suggested stop line
    if sd.get('stop'):
        fig.add_hline(y=sd['stop'], line_color='#dc3545', line_dash='dash',
                      annotation_text=f"Stop ${sd['stop']:.2f}", annotation_position='left',
                      annotation_font_color='#dc3545')

    # Measured target line
    if sd.get('measured_target'):
        fig.add_hline(y=sd['measured_target'], line_color='#28a745', line_dash='dash',
                      annotation_text=f"Target ${sd['measured_target']:.2f}", annotation_position='left',
                      annotation_font_color='#28a745')

    fig.update_layout(_chart_layout(symbol))
    return fig


def _chart_layout(symbol, empty=False):
    return dict(
        title=dict(text=symbol if empty else '', font=dict(color='white')),
        paper_bgcolor='#1e1e2e',
        plot_bgcolor='#1e1e2e',
        font=dict(color='#aaa'),
        xaxis=dict(
            gridcolor='#2a2a3e', showgrid=True, rangeslider=dict(visible=False),
            type='category', nticks=8,
        ),
        yaxis=dict(gridcolor='#2a2a3e', showgrid=True, side='right'),
        legend=dict(orientation='h', y=1.05, x=0),
        margin=dict(l=10, r=60, t=30, b=10),
        height=280,
    )


# ===========================================================================
# Helpers
# ===========================================================================

def _account_figures(acc: dict) -> tuple[float, float]:
    """Return (account_value, settled_cash) from account summary dict."""
    try:
        account_value = float(acc.get('NetLiquidation', 0))
    except Exception:
        account_value = 0.0
    try:
        # SettledCash is the safest to use for new buys (no unsettled funds)
        settled_cash = float(acc.get('SettledCash') or acc.get('TotalCashValue') or 0)
    except Exception:
        settled_cash = 0.0
    return account_value, max(settled_cash, 0.0)


def _fmt_usd(val):
    try:
        return f"${float(val):,.2f}"
    except Exception:
        return '—'


def _fmt_usd_signed(val):
    try:
        v = float(val)
        color = '#28a745' if v >= 0 else '#dc3545'
        sign = '+' if v >= 0 else ''
        return html.Span(f"{sign}${v:,.2f}", style={'color': color})
    except Exception:
        return '—'


def _order_preview_body(action, symbol, qty, price, tp, sl, usd,
                        tp_pct, sl_pct, stage_label, sizing_reason=None):
    if action == 'BUY':
        profit = round(usd * (tp_pct / 100), 2)
        loss   = round(usd * (sl_pct / 100), 2)

        body = html.Div([
            # What you're buying
            dbc.Row(className='mb-3', children=[
                dbc.Col([
                    html.Div(html.Strong(symbol, style={'fontSize': '1.6rem', 'color': 'white'})),
                    html.Div(html.Span(stage_label, className='badge',
                                       style={'backgroundColor': '#28a745', 'fontSize': '0.9rem'})),
                ]),
            ]),

            html.Hr(style={'borderColor': '#444'}),

            # The three numbers that matter
            dbc.Row(className='text-center my-3 g-2', children=[
                dbc.Col(dbc.Card(style={'backgroundColor': '#1a1a2e', 'border': '1px solid #444'}, children=dbc.CardBody([
                    html.Div('You spend', className='text-secondary', style={'fontSize': '0.8rem'}),
                    html.Div(f'${usd:,.2f}', style={'fontSize': '1.5rem', 'fontWeight': 'bold', 'color': 'white'}),
                    html.Div(f'{qty:,.4f} shares @ ${price:.2f}', className='text-secondary', style={'fontSize': '0.75rem'}),
                ]))),
                dbc.Col(dbc.Card(style={'backgroundColor': '#0d2b0d', 'border': '1px solid #28a745'}, children=dbc.CardBody([
                    html.Div('Best case profit', className='text-secondary', style={'fontSize': '0.8rem'}),
                    html.Div(f'+${profit:,.2f}', style={'fontSize': '1.5rem', 'fontWeight': 'bold', 'color': '#28a745'}),
                    html.Div(f'Auto-sells at ${tp:.2f} (+{tp_pct:.0f}%)', className='text-secondary', style={'fontSize': '0.75rem'}),
                ]))),
                dbc.Col(dbc.Card(style={'backgroundColor': '#2b0d0d', 'border': '1px solid #dc3545'}, children=dbc.CardBody([
                    html.Div('Worst case loss', className='text-secondary', style={'fontSize': '0.8rem'}),
                    html.Div(f'-${loss:,.2f}', style={'fontSize': '1.5rem', 'fontWeight': 'bold', 'color': '#dc3545'}),
                    html.Div(f'Auto-sells at ${sl:.2f} (-{sl_pct:.0f}%)', className='text-secondary', style={'fontSize': '0.75rem'}),
                ]))),
            ]),

            html.Hr(style={'borderColor': '#444'}),

            # Sizing reason
            html.Div(
                html.Small(f'Sizing: {sizing_reason}', className='text-secondary'),
                className='mb-2',
            ) if sizing_reason else html.Div(),

            dbc.Alert(
                '⚠  This sends a LIVE order to IBKR TWS.',
                color='warning', className='mb-0 py-2 text-center',
            ),
        ])

    else:  # SELL
        body = html.Div([
            dbc.Row(className='mb-3', children=[
                dbc.Col([
                    html.Div(html.Strong(symbol, style={'fontSize': '1.6rem', 'color': 'white'})),
                    html.Div(html.Span(stage_label or 'Sell', className='badge bg-danger',
                                       style={'fontSize': '0.9rem'})),
                ]),
            ]),
            html.Hr(style={'borderColor': '#444'}),
            dbc.Row(className='text-center my-3', children=[
                dbc.Col(dbc.Card(style={'backgroundColor': '#1a1a2e', 'border': '1px solid #dc3545'}, children=dbc.CardBody([
                    html.Div('You sell', className='text-secondary', style={'fontSize': '0.8rem'}),
                    html.Div(f'${usd:,.2f}', style={'fontSize': '1.5rem', 'fontWeight': 'bold', 'color': 'white'}),
                    html.Div(f'{qty:,.4f} shares at market price', className='text-secondary', style={'fontSize': '0.75rem'}),
                ]))),
            ]),
            html.Hr(style={'borderColor': '#444'}),
            dbc.Alert('⚠  This sends a LIVE order to IBKR TWS.', color='warning', className='mb-0 py-2 text-center'),
        ])

    return body
