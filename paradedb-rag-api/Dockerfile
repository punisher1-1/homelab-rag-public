FROM python:3.12-slim

WORKDIR /app

# Python buffers stdout when it is not a TTY, so print() output can sit in a
# buffer instead of reaching `docker logs`. Every diagnostic this service emits
# — [rerank] skipped, [stream] upstream failed — is a print(), so without this
# they are effectively invisible. Found 2026-07-30.
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

# The service listens on 8100 inside the container.
EXPOSE 8100
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100"]
