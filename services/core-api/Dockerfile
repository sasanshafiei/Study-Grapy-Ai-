FROM python:3.11-slim

WORKDIR /app

# The base image's packaging tools (pip 24.0, setuptools 79.0.1) carry published
# advisories; these releases fix them. They are installer tools, not runtime
# dependencies of the application.
RUN pip install --no-cache-dir --upgrade pip==26.3 setuptools==84.2.1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# --reload is for development; the mounted volume makes code changes hot-reload.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
