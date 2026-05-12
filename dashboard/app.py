import dash
import dash_bootstrap_components as dbc
from dash import dcc, html

from analysis.stages import STAGE_LABELS, STAGE_COLORS
import config


def create_app(store, worker) -> dash.Dash:
    app = dash.Dash(
        __name__,
        external_stylesheets=[dbc.themes.DARKLY],
        title='Stage Trader',
        suppress_callback_exceptions=True,
    )

    app.layout = dbc.Container(
        fluid=True,
        className='px-3 py-2',
        style={'minHeight': '100vh', 'backgroundColor': '#111'},
        children=[
            # ---- Header -----------------------------------------------
            dbc.Row(className='align-items-center mb-2', children=[
                dbc.Col(width='auto', children=[
                    html.H4('Stage Trader', className='text-white mb-0 fw-bold'),
                    html.Small('Stan Weinstein Stage Analysis', className='text-secondary'),
                ]),
                dbc.Col(children=_stat_cards()),
                dbc.Col(width='auto', children=[
                    html.Span(id='conn-badge', children='●  Connecting…',
                              className='badge fs-6', style={'backgroundColor': '#555'}),
                    html.Small(id='last-update', className='text-secondary ms-2'),
                ]),
            ]),

            # ---- Goal progress ----------------------------------------
            dbc.Row(className='mb-2', children=[
                dbc.Col(children=[
                    html.Small(id='goal-label', className='text-secondary'),
                    dbc.Progress(id='goal-bar', value=0, color='success',
                                 style={'height': '6px'}, className='mt-1'),
                ]),
            ]),

            dbc.Alert(id='error-alert', color='danger', dismissable=True,
                      is_open=False, className='py-2 mb-2'),

            # ---- Tabs -------------------------------------------------
            dbc.Tabs(id='tabs', active_tab='portfolio', children=[
                dbc.Tab(label='Portfolio', tab_id='portfolio'),
                dbc.Tab(label='Stage Analysis', tab_id='analysis'),
                dbc.Tab(label='Recommendations & Execute', tab_id='execute'),
                dbc.Tab(label='Order History', tab_id='history'),
            ]),
            html.Div(id='tab-content', className='mt-3'),

            # ---- Order confirmation modal ------------------------------
            dbc.Modal(id='order-modal', size='lg', centered=True, children=[
                dbc.ModalHeader(dbc.ModalTitle('Confirm Order')),
                dbc.ModalBody([
                    # Editable USD amount — debounce=True updates on Enter or click-away
                    dbc.Row(className='mb-3 align-items-center', children=[
                        dbc.Col(width='auto', children=[
                            dbc.Label('USD Amount to invest:', className='mb-0 fw-bold'),
                        ]),
                        dbc.Col(width=3, children=[
                            dbc.Input(
                                id='modal-usd-input', type='number', min=1, step=10,
                                value=config.DEFAULT_TRADE_USD,
                                style={'backgroundColor': '#2a2a3e', 'color': 'white',
                                       'border': '1px solid #444'},
                            ),
                        ]),
                        dbc.Col(width='auto', children=[
                            html.Small('Preview updates as you type',
                                       className='text-secondary'),
                        ]),
                    ]),
                    html.Div(id='order-modal-body'),
                ]),
                dbc.ModalFooter([
                    dbc.Button('Cancel', id='cancel-btn', color='secondary', outline=True),
                    dbc.Button('Execute Order', id='execute-btn', color='success'),
                ]),
            ]),

            # ---- Hidden state ----------------------------------------
            dcc.Store(id='pending-order'),   # holds order params waiting for confirm
            dcc.Store(id='order-result'),    # tracks submitted order id
            dcc.Interval(id='interval', interval=config.REFRESH_SECONDS * 1000,
                         n_intervals=0),

            # Pass store/worker refs into callbacks via app context
            dcc.Store(id='_dummy'),
        ],
    )

    # attach callbacks (import here to avoid circular imports)
    from dashboard import callbacks  # noqa: F401
    callbacks.register(app, store, worker)

    return app


# ---------------------------------------------------------------------------
def _stat_cards():
    def card(label, elem_id, suffix=''):
        return dbc.Col(
            dbc.Card(className='bg-secondary bg-opacity-25 border-0 p-2 text-center', children=[
                html.Div(id=elem_id, className='fs-5 fw-bold text-white'),
                html.Small(label, className='text-secondary'),
            ]),
        )

    return dbc.Row(children=[
        card('Net Liquidation', 'stat-netliq'),
        card('Cash', 'stat-cash'),
        card('Unrealised P&L', 'stat-upnl'),
        card('Daily P&L', 'stat-dpnl'),
    ], className='g-2')
