""" Helper functions """

import re as _re

def normalize_bill_number(raw: str) -> str:
     """ Strips white spaces and leading zeros from numeric suffix 
        e.g. HB 0147" → "HB147" """
     
     s = raw.upper().replace(" ", "")
     return _re.sub(r'([A-Z]+)0*(\d+)', lambda m: m.group(1) + str(int(m.group(2))), s)