"""Independent-review regressions; all records and integrations are local fakes."""
from copy import deepcopy
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import auth, orders, search
from core.security import encode_session, decode_session, hash_password, validate_session_secret
from test_orders import Collection, api
from test_direct_lifecycle import flow


@pytest.mark.parametrize('secret', [None, 'short', 's' * 31, 's' * 32])
def test_secret_startup_and_readiness(monkeypatch, secret):
    import main
    if secret is None:
        monkeypatch.delenv('SESSION_SECRET', raising=False)
    else:
        monkeypatch.setenv('SESSION_SECRET', secret)
    connect, close = AsyncMock(), AsyncMock()
    monkeypatch.setattr(main, 'connect_db', connect)
    monkeypatch.setattr(main, 'close_db', close)
    valid = secret is not None and len(secret) >= 32
    if valid:
        assert validate_session_secret() == secret
        with TestClient(main.app) as client:
            assert client.get('/health').json() == {'status': 'ok'}
            monkeypatch.delenv('SESSION_SECRET')
            assert client.get('/health').status_code == 503
        connect.assert_awaited_once()
        close.assert_awaited_once()
    else:
        with pytest.raises(RuntimeError, match='SESSION_SECRET must contain at least 32 characters'):
            with TestClient(main.app):
                pass
        connect.assert_not_awaited()
        with pytest.raises(RuntimeError):
            encode_session({'role': 'staff', 'userId': 'staff'})
        assert decode_session('invalid') is None
        response = TestClient(main.app).get('/health')
        assert response.status_code == 503
        assert response.json() == {'detail': 'Authentication configuration is invalid'}


@pytest.mark.parametrize('role', ['staff', 'aggregator'])
def test_signed_role_bound_login(monkeypatch, role):
    monkeypatch.setenv('SESSION_SECRET', 'local-test-only-' * 4)
    user = {'_id': 'synthetic-user', 'email': 'test@example.com', 'name': 'Staff',
            'companyName': 'Pharmacy', 'password_hash': hash_password('test-password')}
    db = SimpleNamespace(staff_users=Collection([user]), aggregator_users=Collection([user]))
    monkeypatch.setattr(auth, 'get_db', lambda: db)
    app = FastAPI()
    app.include_router(auth.router, prefix='/api/auth')
    with TestClient(app) as client:
        result = client.post(f'/api/auth/{role}/login', json={'email': user['email'], 'password': 'test-password'})
        assert result.status_code == 200
        token = result.json()['session']
        cookies = SimpleCookie()
        cookies.load(result.headers['set-cookie'])
        assert cookies[f'{role}_session'].value == token
        assert decode_session(token, role)['userId'] == user['_id']
        assert decode_session(token, 'staff' if role == 'aggregator' else 'aggregator') is None


def test_search_role_binding(monkeypatch):
    monkeypatch.setenv('SESSION_SECRET', 'local-test-only-' * 4)
    calls = []
    original = httpx.AsyncClient
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'phone': 'synthetic'})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    app = FastAPI()
    app.include_router(search.router, prefix='/api')
    with TestClient(app) as client:
        for role, expected in [('aggregator', 401), ('staff', 200)]:
            client.cookies.set('staff_session', encode_session({'role': role, 'userId': 'test'}))
            assert client.get('/api/members/synthetic').status_code == expected
            assert len(calls) == (1 if role == 'staff' else 0)


@pytest.mark.parametrize('body', [None, {}])
def test_competitive_accept_legacy_body(flow, body):
    flow.doc.update(status='awaiting_fulfillment', winnerId=str(flow.agg['_id']))
    flow.auth('aggregator')
    kwargs = {} if body is None else {'json': body}
    assert flow.client.post(f"/api/orders/{flow.doc['_id']}/accept", **kwargs).status_code == 200


@pytest.mark.parametrize('body,expected', [(None, 422), ({}, 422), ({'expectedVersion': 0}, 409), ({'expectedVersion': 3}, 200)])
def test_direct_accept_version(flow, body, expected):
    flow.advance('awaiting_fulfillment')
    before = deepcopy(flow.doc)
    flow.auth('aggregator')
    kwargs = {} if body is None else {'json': body}
    assert flow.client.post(f"/api/orders/{flow.doc['_id']}/accept", **kwargs).status_code == expected
    if expected != 200:
        assert flow.doc == before


@pytest.mark.parametrize('action,stage', [('assign', None), ('assign', 'direct_reassignment'),
    ('direct-approve', 'direct_price_review'), ('direct-deny', 'direct_price_review'),
    ('recall', 'accepted'), ('adjust-price', 'completed'), ('cancel', 'completed')])
def test_service_key_cannot_administer(flow, monkeypatch, action, stage):
    monkeypatch.setattr(orders, '_PHARMACY_SERVICE_KEY', 'local-machine-key')
    if stage == 'direct_reassignment':
        flow.advance('accepted')
        assert flow.post('recall', {'reason': 'Retry'}).status_code == 200
    elif stage:
        flow.advance(stage)
    before = deepcopy(flow.doc)
    flow.client.cookies.clear()
    data = {'expectedVersion': flow.doc.get('version', 0), 'reason': 'Test',
            'totalPrice': 900, 'aggregatorId': str(flow.agg['_id'])}
    response = flow.client.post(f"/api/orders/{flow.doc['_id']}/{action}", json=data,
                                headers={'x-service-key': 'local-machine-key'})
    assert response.status_code == 401
    assert flow.doc == before


def test_legacy_machine_access_and_constant_time_comparison(flow, monkeypatch):
    monkeypatch.setattr(orders, '_PHARMACY_SERVICE_KEY', 'local-machine-key')
    original = orders.hmac.compare_digest
    calls = []
    def compare(a, b):
        calls.append((a, b))
        return original(a, b)
    monkeypatch.setattr(orders.hmac, 'compare_digest', compare)
    flow.client.cookies.clear()
    for path in ['/api/aggregators', f"/api/orders/{flow.doc['_id']}"]:
        assert flow.client.get(path, headers={'x-service-key': 'local-machine-key'}).status_code == 200
        assert flow.client.get(path, headers={'x-service-key': 'wrong'}).status_code == 401
        assert flow.client.get(path).status_code == 401
    assert len(calls) == 6


def test_denial_metadata_and_reset(flow):
    flow.advance('direct_price_review')
    assert flow.post('direct-deny', {'reason': '  Too expensive  '}).status_code == 200
    flow.auth()
    detail = flow.client.get(f"/api/orders/{flow.doc['_id']}").json()
    assert detail['denialComment'] == 'Too expensive'
    assert detail['deniedBy'] == {'userId': 'staff', 'name': 'staff'}
    assert detail['deniedAt']
    assert orders._order_summary(flow.doc).denialComment == 'Too expensive'
    audit = deepcopy(flow.doc['history'])
    assert audit[-1]['reason'] == 'Too expensive'
    assert flow.post('assign', {'aggregatorId': str(flow.other['_id'])}).status_code == 200
    assert flow.doc['history'][:-1] == audit
    for key in ['denialComment', 'deniedBy', 'deniedAt']:
        assert flow.doc[key] is None


def test_quote_and_final_price_reporting(flow):
    flow.advance('direct_price_review')
    assert flow.post('direct-approve', {'adjusted_price': 900, 'reason': 'Discount'}).status_code == 200
    assert flow.post('accept', role='aggregator').status_code == 200
    assert flow.post('fulfill', {'fulfillmentType': 'delivered', 'deliveryFee': 50}, 'aggregator').status_code == 200
    assert flow.doc['winnerTotalPrice'] == 950
    assert flow.post('adjust-price', {'totalPrice': 925, 'reason': 'Correction'}).status_code == 200
    flow.auth()
    detail = flow.client.get(f"/api/orders/{flow.doc['_id']}").json()
    summary = orders._order_summary(flow.doc)
    assert detail['directQuote']['totalPrice'] == summary.directQuote['totalPrice'] == 1000
    assert detail['winnerTotalPrice'] == summary.winnerTotalPrice == 925
    approval = next(e for e in detail['history'] if e['eventType'] == 'direct_quote_approved')
    assert approval['newValues']['winnerTotalPrice'] == 900


@pytest.mark.parametrize('action', ['cancel', 'recall'])
@pytest.mark.parametrize('stale', [False, True])
def test_double_terminal_action(flow, action, stale):
    flow.advance()
    version = flow.doc['version']
    assert flow.post(action, {'reason': 'Administrative action'}).status_code == 200
    before = deepcopy(flow.doc)
    result = flow.post(action, {'reason': 'Repeat'}, version=version if stale else None)
    assert result.status_code == 409
    assert 'already terminal' in result.json()['detail']
    assert flow.doc == before


@pytest.mark.parametrize('field', ['unitPrice', 'totalPrice'])
@pytest.mark.parametrize('price', [0, -1, 'NaN', 'Infinity', '-Infinity', 1.25])
def test_competitive_price_validation(flow, field, price):
    assert flow.post('approve').status_code == 200
    before = deepcopy(flow.db.bids.rows)
    response = flow.post('bids', {**{'unitPrice': 1, 'totalPrice': 10}, field: price}, 'aggregator')
    assert response.status_code == (200 if price == 1.25 else 422)
    if price != 1.25:
        assert flow.db.bids.rows == before


def test_deployment_and_test_dependency_configuration():
    root = Path(__file__).resolve().parents[1]
    blueprint = (root / 'render.yaml').read_text()
    assert '      - key: SESSION_SECRET\n        sync: false\n' in blueprint
    assert '    healthCheckPath: /health\n' in blueprint
    production = (root / 'requirements.txt').read_text()
    development = (root / 'requirements-dev.txt').read_text()
    assert 'pytest' not in production
    assert '-r requirements.txt' in development
    assert 'pytest>=' in development and 'pytest-asyncio>=' in development
