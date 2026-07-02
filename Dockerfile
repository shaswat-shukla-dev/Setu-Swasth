FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend ./backend
COPY frontend ./frontend

WORKDIR /app/backend
ENV PORT=8000
EXPOSE 8000

CMD gunicorn main:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:${PORT} --workers 2 --timeout 60
