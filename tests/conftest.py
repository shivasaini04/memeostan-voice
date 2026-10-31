import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Never read developer keys or touch the checkout's database.
os.environ['VOICE_DB'] = '/tmp/voice-tests-unused.db'
os.environ['GEMINI_API_KEY'] = ''
os.environ['SARVAM_API_KEY'] = ''
os.environ['OPENROUTER_API_KEY'] = ''
os.environ['ENABLE_OUTBOUND_CALLS'] = '0'
import pytest
import db
@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'test.db'))
    monkeypatch.setenv('ENABLE_OUTBOUND_CALLS','0')
    monkeypatch.setenv('PUBLIC_ORIGIN','http://testserver')
    monkeypatch.setenv('COOKIE_SECURE','0')
    monkeypatch.setenv('VOICE_TENANT_ID','1')
    db.init_db()

@pytest.fixture(autouse=True)
def no_paid_network(monkeypatch):
    import urllib.request
    import aiohttp
    def deny(*args, **kwargs):
        raise AssertionError('External provider network is forbidden in unit tests')
    async def deny_async(*args, **kwargs):
        raise AssertionError('External provider network is forbidden in unit tests')
    monkeypatch.setattr(urllib.request,'urlopen',deny)
    monkeypatch.setattr(aiohttp.ClientSession,'_request',deny_async)
    import httpx
    monkeypatch.setattr(httpx.AsyncClient,'send',deny_async)
