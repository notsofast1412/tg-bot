import asyncio
from copy import deepcopy
import time

from aiohttp import web
import httpx
import pytest

from paintbot.marketplaces import WildberriesAdapter
from paintbot.marketplaces import wb_browser
from paintbot.marketplaces.links import MarketError, parse_link
from paintbot.services.wb_desktop import Broker, BridgeError, application
from paintbot.services.batch import Batch, collect
from paintbot.scheduler.monitor import Monitor
from paintbot.database.models import NotificationQueue
from sqlalchemy import select


URL = 'https://www.wildberries.ru/catalog/518397236/detail.aspx?size=716573024'
PRODUCT = {'id': 518397236, 'name': 'Краска автомобильная 8017, 400 мл', 'totalQuantity': 52,
           'sizes': [{'optionId': 716573024, 'price': {'basic': 51600, 'product': 26300},
                      'stocks': [{'qty': 52}], 'payload': 'PRIVATE'}], 'token': 'PRIVATE'}
SESSION = 'a' * 32


async def start_job(broker):
    broker.claim(SESSION)
    task = asyncio.create_task(broker.fetch(URL))
    await asyncio.sleep(0)
    claim = broker.claim(SESSION)
    result = {'job_id': claim['id'], 'session': SESSION, 'page_url': URL,
              'started_at': time.time(), 'status': 200, 'source': wb_browser.SOURCE,
              'age_seconds': 0, 'products': [deepcopy(PRODUCT)], 'dest': '1259570991'}
    return task, result


async def test_bridge_sanitizes_real_product_shape_and_adapter_selects_option(tmp_path, monkeypatch):
    broker = Broker(timeout=1)
    task, result = await start_job(broker)
    broker.submit(result)
    report = await task
    assert 'PRIVATE' not in repr(report)
    key = tmp_path / 'key'
    key.write_text('a' * 64)
    monkeypatch.setattr(wb_browser, 'KEY_PATH', key)
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=report)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        card = await WildberriesAdapter(client=client, gap=0).fetch(parse_link(URL))
    assert card.price == 26300
    assert card.card_id == '518397236|size:716573024'
    assert card.attributes['WB dest'] == '1259570991'
    assert 'без кошелька' in card.source
    assert [str(r.url) for r in requests] == [wb_browser.ENDPOINT + '/fetch']


@pytest.mark.parametrize('case', ['session', 'job', 'url', 'age', 'nan', 'old', 'future', 'source', 'product', 'variant'])
async def test_invalid_extension_results_cannot_complete_job(case):
    broker = Broker(timeout=1)
    task, result = await start_job(broker)
    original = deepcopy(result)
    if case == 'session': result['session'] = 'b' * 32
    elif case == 'job': result['job_id'] = 'b' * 32
    elif case == 'url': result['page_url'] = URL.replace('716573024', '999')
    elif case == 'age': result['age_seconds'] = 121
    elif case == 'nan': result['age_seconds'] = float('nan')
    elif case == 'old': result['started_at'] = time.time() - 100
    elif case == 'future': result['started_at'] = time.time() + 100
    elif case == 'source': result['source'] = 'https://evil.test/'
    elif case == 'product': result['products'][0]['id'] = 999
    elif case == 'variant': result['products'][0]['sizes'][0]['optionId'] = True
    with pytest.raises(BridgeError): broker.submit(result)
    assert not task.done()
    broker.submit(original)
    assert (await task)['url'] == URL


@pytest.mark.parametrize('status,kind,error', [(498, 'document', 'page_refused'),
    (403, 'api', 'api_refused'), (429, 'api', 'rate_limit')])
async def test_real_browser_refusals_are_errors(status, kind, error):
    broker = Broker(timeout=1)
    task, result = await start_job(broker)
    result.update(status=status, kind=kind)
    broker.submit(result)
    with pytest.raises(BridgeError, match=error): await task
    assert broker.active is None


async def test_timeout_and_cancellation_release_job_and_reject_late_result():
    broker = Broker(timeout=0.01)
    task, result = await start_job(broker)
    with pytest.raises(BridgeError, match='timeout'): await task
    with pytest.raises(BridgeError): broker.submit(result)
    task, result = await start_job(broker)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert broker.active is None
    with pytest.raises(BridgeError): broker.submit(result)


async def test_missing_extension_and_busy_browser_do_not_start_extra_jobs():
    broker = Broker(timeout=1)
    with pytest.raises(BridgeError, match='extension_missing'): await broker.fetch(URL)
    task, result = await start_job(broker)
    with pytest.raises(BridgeError, match='busy'): await broker.fetch(URL)
    broker.submit(result)
    await task


async def test_real_loopback_transport_authentication_and_end_to_end_job(tmp_path, monkeypatch):
    key = tmp_path / 'key'
    key.write_text('a' * 64)
    monkeypatch.setattr(wb_browser, 'KEY_PATH', key)
    navigated = []
    async def navigate(url):
        navigated.append(url)
    runner = web.AppRunner(application(key.read_text(), navigate=navigate), access_log=None, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    monkeypatch.setattr(wb_browser, 'ENDPOINT', url)
    headers = {'Authorization': 'Bearer ' + key.read_text()}
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(url + '/job?session=' + SESSION)).status_code == 403
            assert (await client.get(url + '/health', headers={**headers, 'Host': 'evil.test'})).status_code == 403
            origin = 'chrome-extension://' + 'a' * 32
            preflight = await client.options(url + '/job', headers={'Origin': origin,
                'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'authorization'})
            assert preflight.status_code == 204
            assert preflight.headers['Access-Control-Allow-Origin'] == origin
            assert (await client.options(url + '/job', headers={'Origin': 'https://www.wildberries.ru'})).status_code == 403
            health = await client.get(url + '/health', headers={**headers, 'Origin': origin})
            assert health.headers['Access-Control-Allow-Origin'] == origin
            old = await client.get(url + '/job?session=' + SESSION, headers=headers)
            assert old.json()['reload_extension'] is True
            await client.get(url + '/job?session=' + SESSION + '&v=3', headers=headers)
            adapter = WildberriesAdapter(client=client, gap=0)
            pending = asyncio.create_task(adapter.fetch(parse_link(URL)))
            try:
                for _ in range(100):
                    response = await client.get(url + '/job?session=' + SESSION + '&v=3', headers=headers)
                    job = response.json()['job']
                    if job: break
                    await asyncio.sleep(0.01)
                assert job
                invalid_navigation = await client.post(url + '/navigate', json={'job_id': job['id'],
                    'session': 'wrong'}, headers=headers)
                assert invalid_navigation.status_code == 503 and not navigated
                for _ in range(2):
                    native = await client.post(url + '/navigate', json={'job_id': job['id'],
                        'session': SESSION, 'url': 'https://evil.test/'}, headers=headers)
                    assert native.status_code == 200
                assert navigated == [URL]
                progress = {'job_id': job['id'], 'session': SESSION, 'event': 'reload_requested',
                            'cookie': 'PRIVATE', 'status': 200}
                assert (await client.post(url + '/progress', json=progress, headers=headers)).status_code == 200
                diagnostic = (await client.get(url + '/health', headers=headers)).json()
                assert diagnostic['extension_revision'] == 3
                assert diagnostic['trace'][-1] == {'event': 'reload_requested', 'status': 200}
                assert 'PRIVATE' not in repr(diagnostic)
                result = {'job_id': job['id'], 'session': SESSION, 'page_url': URL,
                    'started_at': time.time(), 'status': 200, 'source': wb_browser.SOURCE,
                    'age_seconds': 0, 'products': [PRODUCT], 'dest': '1259570991'}
                assert (await client.post(url + '/result', json=result, headers=headers)).status_code == 200
                assert (await pending).price == 26300
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    finally:
        await runner.cleanup()


@pytest.mark.parametrize('case', ['wrong_size', 'no_stock', 'conflict', 'two_sizes'])
async def test_browser_does_not_use_wallet_neighbor_or_conflicting_price(tmp_path, monkeypatch, case):
    key = tmp_path / 'key'
    key.write_text('a' * 64)
    monkeypatch.setattr(wb_browser, 'KEY_PATH', key)
    product = deepcopy(PRODUCT)
    if case == 'wrong_size': product['sizes'][0]['optionId'] = 999
    elif case == 'no_stock': product['sizes'][0]['stocks'] = []
    elif case == 'two_sizes':
        product['sizes'][0].pop('stocks')
        product['sizes'].append({'optionId': 999, 'price': {'product': 100}})
    report = {'schema': 1, 'url': URL, 'products': [product]}
    if case == 'conflict':
        other = deepcopy(product)
        other['sizes'][0]['price']['product'] = 99900
        report['products'].append(other)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=report))) as client:
        card = await WildberriesAdapter(client=client, gap=0).fetch(parse_link(URL))
    assert card.price is None and card.warning


async def test_browser_rate_limit_is_preserved_and_has_no_direct_fallback(tmp_path, monkeypatch):
    key = tmp_path / 'key'
    key.write_text('a' * 64)
    monkeypatch.setattr(wb_browser, 'KEY_PATH', key)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503, json={'error': 'rate_limit'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = WildberriesAdapter(client=client, gap=0)
        for _ in range(2):
            with pytest.raises(MarketError) as error: await adapter.fetch(parse_link(URL))
            assert error.value.retry_after >= 599
    assert len(calls) == 1


async def test_browser_preview_and_fresh_monitoring_reach_database_and_queue(store, tmp_path, monkeypatch):
    key = tmp_path / 'key'
    key.write_text('a' * 64)
    monkeypatch.setattr(wb_browser, 'KEY_PATH', key)
    product = deepcopy(PRODUCT)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'schema': 1, 'url': URL, 'products': [product]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = WildberriesAdapter(client=client, gap=0)
        batch = Batch()
        await collect(URL, batch, {'wb': adapter}, store)
        assert not batch.errors and batch.cards[0].price == 26300
        ids, _ = store.add_batch(batch.cards, 'Краска', store.tags()[0].id)
        assert store.product(ids[0]).last_price is None
        product['sizes'][0]['price']['product'] = 26000
        assert await Monitor(store, {'wb': adapter}).cycle() == 1
        assert store.product(ids[0]).last_price == 26000
        changes, errors = store.history(ids[0])
        assert [c.price for c in changes] == [26000] and not errors
        with store.tx() as session:
            assert len(list(session.scalars(select(NotificationQueue)))) == 1
    assert len(calls) == 2


async def test_progress_rejects_untrusted_events_and_reports_navigation_failure():
    broker = Broker(timeout=1)
    task, result = await start_job(broker)
    for fields in ({'session': 'wrong'}, {'event': 'PRIVATE'}, {'job_id': 'wrong'}):
        report = {**result, 'event': 'reload_requested', **fields}
        with pytest.raises(BridgeError): broker.progress(report)
    broker.progress({**result, 'event': 'navigation_failed'})
    with pytest.raises(BridgeError, match='navigation_failed'): await task
    assert broker.trace[-1]['event'] == 'navigation_failed'
