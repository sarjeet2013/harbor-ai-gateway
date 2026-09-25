FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY edge ./edge
COPY config ./config
ENV PYTHONUNBUFFERED=1
EXPOSE 4000
CMD ["python", "-m", "uvicorn", "edge.main:app", "--host", "0.0.0.0", "--port", "4000"]
