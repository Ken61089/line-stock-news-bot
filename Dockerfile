FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# LINE 機器人是 Web 服務,需要對外開 port(Zeabur 會自動帶入 PORT)
EXPOSE 8080
CMD ["python", "-u", "bot.py"]
