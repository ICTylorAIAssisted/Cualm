FROM python:3.12-slim

RUN pip install --no-cache-dir slixmpp docker aiohttp

COPY main.py /app/main.py

WORKDIR /app
ENTRYPOINT ["python3", "main.py"]
