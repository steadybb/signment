# Use Python 3.11 slim base image for a lightweight container
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies for psycopg2-binary and eventlet
RUN apt-get update && apt-get install -y \
    gcc \
    libc-dev \
    libpq-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements.txt and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install honcho to manage Procfile processes
RUN pip install --no-cache-dir honcho==2.0.0

# Copy application files
COPY app.py .
COPY bot.py .
COPY worker.py .
COPY utils.py .
COPY forms.py .
COPY simulator_engine.py .
COPY Procfile .
COPY templates/ templates/
COPY static/ static/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Expose port for Flask app
EXPOSE $PORT

# Run Procfile with honcho
# Environment variables (e.g., TELEGRAM_BOT_TOKEN, REDIS_URL, SMTP_HOST, RECAPTCHA_SITE_KEY) should be set via deployment platform config
CMD ["honcho", "start", "-f", "Procfile"]
