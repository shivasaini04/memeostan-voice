"""Explicit CLI test call using the operator's database credentials."""
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).with_name('.env'))
import db
from dialer import place_call, tenant_telephony

if __name__ == '__main__':
    db.init_db()
    phone = input('Allowlisted number you own: ').strip()
    if input('This places a real paid call. Type CALL: ') != 'CALL':
        raise SystemExit('Cancelled.')
    result = place_call(tenant_telephony(int(os.getenv('VOICE_TENANT_ID','1'))), phone, {'lead_name':'Test caller'})
    print('Carrier accepted call.' if result['ok'] else result.get('error', 'Call failed.'))
