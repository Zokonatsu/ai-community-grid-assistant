FROM python:3.14-slim
WORKDIR /app
RUN groupadd -r appgroup -g 1000 && useradd -r -u 1000 -g appgroup appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chown -R appuser:appgroup /app
EXPOSE 8000
USER appuser
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
