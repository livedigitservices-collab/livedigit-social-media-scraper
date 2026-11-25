FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8501

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY livedigit ./livedigit

EXPOSE 8501
CMD ["sh", "-c", "exec streamlit run app.py --server.address=0.0.0.0 --server.port=\"${PORT}\" --server.headless=true --browser.gatherUsageStats=false"]
