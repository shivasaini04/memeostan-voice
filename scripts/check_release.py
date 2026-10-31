"""Check the release source for secrets and accidental private artifacts."""
import json
from pathlib import Path
import subprocess
import sys
root=Path(__file__).resolve().parents[1]
result=subprocess.run([sys.executable,'-m','detect_secrets','scan','--all-files','--exclude-files',r'(^|/)(\.git|\.pytest_cache|__pycache__)/'],cwd=root,text=True,capture_output=True,check=True)
findings=json.loads(result.stdout)['results']
if findings:
    for name, rows in findings.items():
        print(name,[(r['line_number'],r['type']) for r in rows])
    raise SystemExit('Secret scan needs review')
files=subprocess.check_output(['git','ls-files'],cwd=root,text=True).splitlines()
for name in files:
    p=Path(name)
    if p.name=='.env' or p.suffix in {'.db','.wav','.exe'} or 'logs' in p.parts:
        raise SystemExit('Private/generated artifact in release: '+name)
print('No secret findings or tracked private runtime artifacts.')
