# Xauron AI Institutional Scalping v3
# Multi-Timeframe + Auto Signal

import os
import asyncio
import requests
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "")

AUTO_SYMBOLS = ["XAUUSD","BTCUSDT"]
AUTO_TFS = ["1m","5m","15m"]

last_signal_time = {}

# ================= DATA =================

def fetch_twelvedata(symbol, interval="1m", limit=200):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol.replace("USD","/USD"),
        "interval": interval,
        "outputsize": limit,
        "apikey": TWELVEDATA_API_KEY
    }
    r = requests.get(url, params=params)
    j = r.json()
    values = j["values"][::-1]
    c = [float(x["close"]) for x in values]
    h = [float(x["high"]) for x in values]
    l = [float(x["low"]) for x in values]
    return h,l,c

# ================= INDICATORS =================

def ema(data, period):
    k = 2/(period+1)
    e = data[0]
    out=[]
    for v in data:
        e=v*k+e*(1-k)
        out.append(e)
    return out

def atr(h,l,c,period=14):
    trs=[]
    for i in range(len(c)):
        if i==0:
            tr=h[i]-l[i]
        else:
            tr=max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1]))
        trs.append(tr)
    return sum(trs[-period:])/period

# ================= AI CORE =================

def analyze(symbol, tf):

    h,l,c=fetch_twelvedata(symbol,tf)

    ema9=ema(c,9)[-1]
    ema21=ema(c,21)[-1]

    trend="BUY" if ema9>ema21 else "SELL"

    atrv=atr(h,l,c)

    entry=c[-1]

    if trend=="BUY":
        stop=entry-atrv
        tp=entry+2*atrv
    else:
        stop=entry+atrv
        tp=entry-2*atrv

    confidence=80+abs(ema9-ema21)/atrv*5

    return trend,entry,stop,tp,int(min(confidence,95))

# ================= MULTI TIMEFRAME =================

def analyze_mtf(symbol):

    results=[analyze(symbol,tf) for tf in AUTO_TFS]

    trends=[r[0] for r in results]

    if trends.count("BUY")==3:
        trend="BUY"
    elif trends.count("SELL")==3:
        trend="SELL"
    else:
        return None

    entry=results[0][1]
    stop=results[0][2]
    tp=results[0][3]
    conf=sum([r[4] for r in results])/3

    return trend,entry,stop,tp,int(conf)

# ================= TELEGRAM =================

async def send_signal(app,symbol,res):

    trend,entry,stop,tp,conf=res

    msg=f"""
🧠 Xauron AI Institutional

Symbol: {symbol}
Signal: {trend}
Confidence: {conf}%

Entry: {entry:.2f}
Stop: {stop:.2f}
TP: {tp:.2f}

Time: {datetime.now(timezone.utc)}
"""

    for chat_id in last_signal_time:
        await app.bot.send_message(chat_id,msg)

async def auto_loop(app):

    while True:

        for symbol in AUTO_SYMBOLS:

            res=analyze_mtf(symbol)

            if res:

                key=f"{symbol}_{res[0]}"

                if key not in last_signal_time or (datetime.now()-last_signal_time[key]).seconds>300:

                    last_signal_time[key]=datetime.now()

                    await send_signal(app,symbol,res)

        await asyncio.sleep(30)

# ================= HANDLERS =================

async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):

    chat_id=update.effective_chat.id
    last_signal_time[chat_id]=datetime.now()

    await update.message.reply_text("Xauron AI Auto Signal Activated")

# ================= MAIN =================

async def main():

    app=Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",start))

    asyncio.create_task(auto_loop(app))

    await app.run_polling()

if __name__=="__main__":
    asyncio.run(main())
