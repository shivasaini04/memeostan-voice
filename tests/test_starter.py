import asyncio
import base64
import struct
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import db
from contact_gate import run_contact_gate
from dialer import place_call, tenant_telephony
from audio import resample_pcm, rms
from server import app

def operator():
    result=db.signup('Example','operator@example.com','test-password-long')
    return result, db.create_session(result['user_id'])

def test_fail_closed_dialing(monkeypatch):
    with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
        assert not place_call({}, '+919000000001', {})['ok']
        monkeypatch.setenv('ENABLE_OUTBOUND_CALLS','1')
        assert run_contact_gate('+919000000001', True).rejection_code=='NOT_ALLOWLISTED'
        monkeypatch.setenv('ALLOWED_TEST_NUMBERS','+919000000001')
        assert run_contact_gate('+919000000001', True).is_approved
        monkeypatch.setenv('SUPPRESSED_NUMBERS','+919000000001')
        assert run_contact_gate('+919000000001', True).rejection_code=='SUPPRESSED'

def test_credentials_never_fall_back(monkeypatch):
    result,_=operator()
    monkeypatch.setenv('EXOTEL_API_KEY','must-not-be-used')
    assert tenant_telephony(result['tenant_id'])['api_key']==''

def test_auth_csrf_disabled_surfaces_and_health():
    result,token=operator()
    with TestClient(app) as client:
        assert client.get('/health').json()=={'status':'ok','edition':'experimental-starter'}
        assert client.get('/api/v1/engines').status_code==401
        assert client.get('/signup').status_code==404
        assert client.post('/api/v1/voice/turn',json={'text':'hello'}).status_code==404
        assert client.post('/login',data={'email':'x','password':'x'},headers={'origin':'https://evil.example'}).status_code==403
        login=client.post('/login',data={'email':'operator@example.com','password':'test-password-long'},headers={'origin':'http://testserver'},follow_redirects=False)
        assert login.status_code==303
        assert 'HttpOnly' in login.headers['set-cookie']
        assert client.get('/app').status_code==200
        assert client.get('/api/v1/engines').status_code==200
        assert client.post('/app/agent',data={},headers={'origin':'https://evil.example'}).status_code==403
        assert client.post('/api/v1/trigger',json={'phone':'+919000000001'}).status_code==401
        key=db.ensure_api_key(result['tenant_id'])
        assert client.post('/api/v1/trigger',json={'phone':'+919000000001'},headers={'x-api-key':key}).status_code==422

def test_websockets_reject_missing_auth_and_cross_origin():
    _,token=operator()
    with TestClient(app) as client:
        for path in ['/api/v1/voice/live','/api/v1/telephony/exotel/stream']:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(path,headers={'origin':'http://testserver'}): pass
        client.cookies.set('vsa_session',token)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/api/v1/voice/live',headers={'origin':'https://evil.example'}): pass

def test_authenticated_exotel_path_is_bounded(monkeypatch):
    monkeypatch.setenv('EXOTEL_STREAM_USER','voicebot')
    monkeypatch.setenv('EXOTEL_STREAM_PASSWORD','x'*40)
    # Confirm correct Basic auth reaches the handler without invoking a provider.
    called=[]
    async def fake(ws, rate):
        called.append(rate)
        await ws.close()
    with patch('server._exotel_gemini_stream',fake):
        with TestClient(app) as client:
            auth='Basic '+base64.b64encode(('voicebot:'+('x'*40)).encode()).decode()
            with client.websocket_connect('/api/v1/telephony/exotel/stream',headers={'authorization':auth}): pass
    assert called==[8000]

def test_tenant_read_isolation_and_queue_idempotency():
    result,_=operator()
    other=db.create_tenant('Other')
    cid=db.start_call(result['tenant_id'],channel='browser')
    assert db.call_detail(other,cid)[0] is None
    one=db.enqueue_call(result['tenant_id'],'+919000000001',trigger_key='event-1')
    two=db.enqueue_call(result['tenant_id'],'+919000000001',trigger_key='event-1')
    assert two['duplicate'] and one['id']==two['id']
    assert db.claim_next_call()['id']==one['id']
    assert db.claim_next_call() is None

def test_audio_streaming_resampling():
    pcm=struct.pack('<800h', *([1000]*800))
    first,state=resample_pcm(pcm,8000,16000)
    second,_=resample_pcm(pcm,8000,16000,state)
    assert 6300 <= len(first)+len(second) <= 6400
    assert rms(first)==1000

def test_secure_cookie_and_request_size(monkeypatch):
    operator()
    monkeypatch.setenv('COOKIE_SECURE','1')
    with TestClient(app) as client:
        response=client.post('/login',data={'email':'operator@example.com','password':'test-password-long'},headers={'origin':'http://testserver'},follow_redirects=False)
        assert 'Secure' in response.headers['set-cookie']
        assert client.post('/login',content=b'x'*65537,headers={'origin':'http://testserver'}).status_code==413

def test_session_duration_limit(monkeypatch):
    monkeypatch.setenv('EXOTEL_STREAM_USER','voicebot')
    monkeypatch.setenv('EXOTEL_STREAM_PASSWORD','y'*40)
    monkeypatch.setenv('MAX_CALL_SECONDS','1')
    cancelled=[]
    async def fake(ws,rate):
        try: await asyncio.sleep(10)
        finally: cancelled.append(True)
    with patch('server._exotel_gemini_stream',fake):
        with TestClient(app) as client:
            auth='Basic '+base64.b64encode(('voicebot:'+('y'*40)).encode()).decode()
            with client.websocket_connect('/api/v1/telephony/exotel/stream',headers={'authorization':auth}) as ws:
                with pytest.raises(WebSocketDisconnect): ws.receive_text()
    assert cancelled==[True]

def test_recordings_and_email_opt_in(monkeypatch,tmp_path):
    import call_log
    from sales_tools import _smtp_config
    monkeypatch.setattr(call_log,'RECORD_CALLS',False)
    monkeypatch.setattr(call_log,'CALL_DIR',str(tmp_path/'recordings'))
    recorder=call_log.CallRecorder('test',8000)
    recorder.set_rate(16000)
    recorder.write_in(b'\0'*320)
    recorder.note('transcript',text='private')
    recorder.close({})
    assert not (tmp_path/'recordings').exists()
    monkeypatch.setenv('ENABLE_EMAIL','0')
    monkeypatch.setenv('EMAIL_SERVER_HOST','must-not-be-used')
    assert _smtp_config()['host']==''

def test_booking_uniqueness():
    result,_=operator()
    db.create_booking(result['tenant_id'],'2030-01-01 10:00:00','2030-01-01 10:15:00')
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.create_booking(result['tenant_id'],'2030-01-01 10:00:00','2030-01-01 10:15:00')

def test_oversized_audio_closes_before_handler_processes_it(monkeypatch):
    monkeypatch.setenv('EXOTEL_STREAM_USER','voicebot')
    monkeypatch.setenv('EXOTEL_STREAM_PASSWORD','z'*40)
    async def fake(ws,rate):
        await ws.receive()
    with patch('server._exotel_gemini_stream',fake):
        with TestClient(app) as client:
            auth='Basic '+base64.b64encode(('voicebot:'+('z'*40)).encode()).decode()
            with client.websocket_connect('/api/v1/telephony/exotel/stream',headers={'authorization':auth}) as ws:
                ws.send_bytes(b'x'*262145)
                with pytest.raises(WebSocketDisconnect): ws.receive_text()

def test_recording_path_traversal_rejected():
    result,token=operator()
    cid=db.start_call(result['tenant_id'],channel='phone',recording_dir='../../outside')
    with TestClient(app) as client:
        client.cookies.set('vsa_session',token)
        assert client.get(f'/app/calls/{cid}/audio/in').status_code==404
        assert client.get(f'/app/calls/{cid}').status_code==200

def test_gemini_sdk_configuration_is_valid_without_network():
    from engines import GeminiLiveEngine
    from types import SimpleNamespace
    captured=[]
    class Context:
        async def __aenter__(self): return 'fake-session'
    def connect(**kwargs):
        captured.append(kwargs)
        return Context()
    fake_client=SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect)))
    with patch('google.genai.Client', return_value=fake_client):
        engine=GeminiLiveEngine('Example company facts')
        assert asyncio.run(engine._connect_once())=='fake-session'
    assert captured[0]['config'].response_modalities==['AUDIO']

def test_authenticated_browser_ignores_tenant_query_and_cleans_session(monkeypatch):
    result,token=operator()
    monkeypatch.setenv('GEMINI_API_KEY','testing')
    captured=[]
    async def fake(ws,convo,engine,*args):
        captured.append(engine.tool_context['tenant_id'])
        await ws.close()
    async def no_classification(*args): pass
    from voice_session import _SESSIONS
    before=set(_SESSIONS)
    with patch('server._run_gemini_live',fake), patch('server._classify_call',no_classification):
        with TestClient(app) as client:
            client.cookies.set('vsa_session',token)
            with client.websocket_connect('/api/v1/voice/live?engine=gemini&tenant_id=999',headers={'origin':'http://testserver'}): pass
    assert captured==[result['tenant_id']]
    assert set(_SESSIONS)==before

def test_synthetic_exotel_protocol_audio_and_call_record(monkeypatch):
    from engines import SpeechChunk
    operator()
    monkeypatch.setenv('EXOTEL_STREAM_USER','voicebot')
    monkeypatch.setenv('EXOTEL_STREAM_PASSWORD','w'*40)
    class FakeEngine:
        def __init__(self,*args,**kwargs):
            self.tool_context=kwargs.get('tool_context',{})
            self.greeting=''
        async def connect(self): pass
        async def open_with(self,text): pass
        async def seed_said(self,text): pass
        async def send_audio(self,data): pass
        async def close(self): pass
        async def receive(self):
            yield SpeechChunk('Synthetic greeting', b'\x01\x00'*4800,24000,final=True)
    async def no_classification(*args):pass
    auth='Basic '+base64.b64encode(('voicebot:'+('w'*40)).encode()).decode()
    with patch('engines.GeminiLiveEngine',FakeEngine), patch('server._classify_call',no_classification):
        with TestClient(app) as client:
            with client.websocket_connect('/api/v1/telephony/exotel/stream',headers={'authorization':auth}) as ws:
                ws.send_json({'event':'start','start':{'stream_sid':'synthetic','call_sid':'synthetic','from':'','media_format':{'sample_rate':16000}}})
                frame=ws.receive_json()
                assert frame['event']=='media'
                payload=base64.b64decode(frame['media']['payload'])
                assert len(payload)>=3200 and len(payload)%320==0
                ws.send_json({'event':'media','media':{'payload':base64.b64encode(b'\0'*3200).decode()}})
                ws.send_json({'event':'stop'})
    rows=db.recent_calls(1)
    assert len(rows)==1 and rows[0]['status']=='completed'
