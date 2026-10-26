"""Fetch ~640 daily closes per symbol from the Tencent kline endpoint into a local JSON.

Analysis input only: the game ships fitted parameters, never this data.
"""

import json
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

SYMBOLS = {
    "sh600519": "贵州茅台(大盘)",
    "sh601166": "兴业银行(银行)",
    "sh600276": "恒瑞医药(医药)",
    "sh601012": "隆基绿能(光伏)",
    "sh601005": "重庆钢铁(低价)",
    "sz300750": "宁德时代(创业板)",
    "sz300059": "东方财富(创业板)",
    "sh688981": "中芯国际(科创板)",
    "sz002594": "比亚迪(中盘)",
    "sz000725": "京东方A(低价大量)",
}
URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={},day,,,640,qfq"


def main(out):
    data = {}
    for symbol, label in SYMBOLS.items():
        with urllib.request.urlopen(URL.format(symbol), timeout=10) as response:
            body = json.load(response)
        rows = body["data"][symbol].get("qfqday") or body["data"][symbol].get("day")
        data[symbol] = {
            "label": label,
            "dates": [r[0] for r in rows],
            "close": [float(r[2]) for r in rows],
        }
        print(symbol, label, len(rows), rows[0][0], rows[-1][0])
        time.sleep(0.5)
    with open(out, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False)


if __name__ == "__main__":
    main(sys.argv[1])
