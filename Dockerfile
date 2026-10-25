# Tenure's webhook service: python -m tenure.app on port 8080.
#
#   docker build -t tenure . && docker run --rm -p 8080:8080 \
#     -e TENURE_APP_ID=… -e TENURE_WEBHOOK_SECRET=… \
#     -v ~/.config/tenure/private-key.pem:/secrets/key/private-key.pem:ro tenure
#
# Credentials come from the environment and mounted files at run time, never
# from the image. State (grants, merges, the audit chain) goes to /state,
# which a deployment should back with durable storage.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY tenure ./tenure
RUN pip install ".[sandbox]"

RUN useradd --create-home --uid 10001 app && mkdir -p /state && chown app /state
USER app

ENV TENURE_HOST=0.0.0.0 TENURE_PORT=8080 TENURE_STATE_DIR=/state \
    TENURE_PRIVATE_KEY_PATH=/secrets/key/private-key.pem
EXPOSE 8080
CMD ["python", "-m", "tenure.app"]
