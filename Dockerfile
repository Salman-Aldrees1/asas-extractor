FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer-cached separately from source)
COPY requirements-webapp.txt .
RUN pip install --no-cache-dir -r requirements-webapp.txt

# Copy source
COPY llm_pdf_pipeline/ ./llm_pdf_pipeline/
COPY webapp/ ./webapp/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "webapp.main:app", "--host", "0.0.0.0", "--port", "8000"]
