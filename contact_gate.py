"""Test-number controls, not a regulatory DND or consent service."""
import datetime
import os
import re
from typing import Optional
from zoneinfo import ZoneInfo
from pydantic import BaseModel

class GateCheckResult(BaseModel):
    is_approved: bool
    phone_e164: str
    rejection_code: Optional[str] = None
    reason: Optional[str] = None

def clean_and_validate_indian_phone(raw_phone: str) -> Optional[str]:
    digits = re.sub(r'\D', '', str(raw_phone))
    if len(digits) == 12 and digits.startswith('91'): digits = digits[2:]
    elif len(digits) == 11 and digits.startswith('0'): digits = digits[1:]
    return '+91' + digits if len(digits) == 10 and digits[0] in '6789' else None

def is_trai_calling_hour() -> bool:
    """Legacy function name; checks the example window, not legal compliance."""
    hour = datetime.datetime.now(ZoneInfo('Asia/Kolkata')).hour
    return int(os.getenv('CALL_HOUR_START', '9')) <= hour < int(os.getenv('CALL_HOUR_END', '21'))

def run_contact_gate(raw_phone: str, bypass_time_check_for_dev: bool = False) -> GateCheckResult:
    phone = clean_and_validate_indian_phone(raw_phone)
    def blocked(code, reason):
        return GateCheckResult(is_approved=False, phone_e164=phone or '', rejection_code=code, reason=reason)
    if not phone: return blocked('INVALID_INDIAN_PHONE', 'Enter an Indian mobile number.')
    if os.getenv('ENABLE_OUTBOUND_CALLS', '0') != '1':
        return blocked('OUTBOUND_DISABLED', 'Outbound calling is disabled.')
    allowed = {clean_and_validate_indian_phone(n) for n in os.getenv('ALLOWED_TEST_NUMBERS', '').split(',')}
    suppressed = {clean_and_validate_indian_phone(n) for n in os.getenv('SUPPRESSED_NUMBERS', '').split(',')}
    if phone not in allowed: return blocked('NOT_ALLOWLISTED', 'Number is not in ALLOWED_TEST_NUMBERS.')
    if phone in suppressed: return blocked('SUPPRESSED', 'Number is locally suppressed.')
    if not bypass_time_check_for_dev and not is_trai_calling_hour():
        return blocked('CALLING_WINDOW', 'Outside the configured example calling window.')
    return GateCheckResult(is_approved=True, phone_e164=phone)
