"""
進入點:Zeabur 的 Python 建置器(zbpack)預設執行 `python main.py`,
而非看 Dockerfile,所以提供這個檔案當入口。
直接匯入 bot.py 裡的 Flask `app` 並用 waitress 啟動。
"""

import os
import logging

from waitress import serve

from bot import app  # bot.py 的 Flask app(模組層級就建立好)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    logging.getLogger("line-news-bot").info("LINE 機器人啟動,監聽埠口 %s ...", port)
    serve(app, host="0.0.0.0", port=port)
