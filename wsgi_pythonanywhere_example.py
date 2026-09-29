# Reference only - PythonAnywhere generates its own WSGI file per account at
# /var/www/<your_username>_pythonanywhere_com_wsgi.py. Open that file in their
# web UI and replace its contents with this (adjusting YOUR_USERNAME below),
# rather than adding this file to the actual import path.

import os
import sys

# Adjust to wherever you cloned the repo, e.g. /home/YOUR_USERNAME/stock-scanner
path = "/home/YOUR_USERNAME/stock-scanner"
if path not in sys.path:
    sys.path.insert(0, path)

# Config for this deployment - PythonAnywhere's free tier has a daily CPU-seconds
# quota, so auto-refresh is set to once/day here rather than every 20 min; rely on
# manually clicking Refresh instead. Adjust SCANNER_UNIVERSE/etc. as you like.
os.environ.setdefault("SCANNER_UNIVERSE", "AAPL,MSFT,GOOGL,AMZN,NVDA,META,TSLA,AVGO,ORCL,CRM,ADBE,AMD,CSCO,ACN,IBM,NOW,INTU,TXN,QCOM,AMAT,MU,PANW,SNPS,CDNS,ANET,LRCX,KLAC,APH,ROP,MSI,JPM,V,MA,BAC,WFC,GS,MS,AXP,BLK,SCHW,C,SPGI,PGR,CB,PNC,USB,JNJ,UNH,LLY,ABBV,MRK,PFE,TMO,ABT,DHR,BMY,AMGN,GILD,VRTX,ISRG,SYK,BSX,MDT,ELV,CI,WMT,PG,KO,PEP,COST,HD,MCD,NKE,DIS,CMCSA,LOW,TJX,SBUX,TGT,BKNG,XOM,CVX,COP,SLB,EOG,GE,CAT,RTX,HON,UNP,BA,DE,LMT,UPS,ADP,ETN,AMT,PLD,LIN,NEE,SO,DUK")
os.environ.setdefault("SCANNER_TOP", "40")
os.environ.setdefault("SCANNER_AUTO_REFRESH_SECONDS", "86400")  # once/day - conserve CPU quota

from app import app as application  # noqa: E402
