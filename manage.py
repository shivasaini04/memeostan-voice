"""Local operator provisioning and data deletion; never invokes a provider."""
import argparse
import getpass
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).with_name('.env'))
import db

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('command', choices=['create-operator','configure-exotel','purge-call-data'])
args = parser.parse_args()
db.init_db()
tenant_id = int(os.getenv('VOICE_TENANT_ID', '1'))
if args.command == 'create-operator':
    with db.connect() as conn:
        if conn.execute('SELECT count(*) FROM user').fetchone()[0]:
            raise SystemExit('An operator already exists. This starter supports one operator.')
    company = input('Company name: ').strip()
    email = input('Operator email: ').strip()
    password = getpass.getpass('Password (12+ characters): ')
    if not company or '@' not in email: raise SystemExit('Company and valid email required.')
    result = db.signup(company, email, password)
    if not result['ok']: raise SystemExit(result['error'])
    print('Operator created. Set VOICE_TENANT_ID=' + str(result['tenant_id']) + ' in .env.')
elif args.command == 'configure-exotel':
    if not db.tenant_row(tenant_id): raise SystemExit('Create an operator first.')
    keys = ['EXOTEL_ACCOUNT_SID','EXOTEL_API_KEY','EXOTEL_API_TOKEN','EXOTEL_CALLER_ID','EXOTEL_FLOW_URL']
    values = [getpass.getpass(k + ': ').strip() for k in keys]
    if not all(values): raise SystemExit('All five values are required.')
    with db.connect() as conn:
        conn.execute('UPDATE tenant SET exotel_account_sid=?, exotel_api_key=?, exotel_api_token=?, exotel_caller_id=?, exotel_flow_url=? WHERE id=?', (*values, tenant_id))
    print('Operator Exotel configuration saved locally. Protect the database.')
else:
    if input('Delete all call history, transcripts, outcomes, bookings, queue and recordings? Type DELETE: ') != 'DELETE':
        raise SystemExit('Cancelled.')
    with db.connect() as conn:
        for table in ['outcome','turn','booking','call_queue','call']:
            conn.execute('DELETE FROM ' + table)
    import shutil
    shutil.rmtree(Path(__file__).parent / 'logs', ignore_errors=True)
    with db.connect() as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.execute('VACUUM')
    print('Deleted local call data. Provider-held data and backups require separate deletion.')
