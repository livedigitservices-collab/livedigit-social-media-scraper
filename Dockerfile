FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8501

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=secret,id=pip_ca \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY livedigit ./livedigit

EXPOSE 8501
CMD ["sh", "-c", "exec streamlit run app.py --server.address=0.0.0.0 --server.port=\"${PORT}\" --server.headless=true --browser.gatherUsageStats=false"]
