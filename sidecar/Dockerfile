FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY oauth/ ./oauth/
COPY datastores/ ./datastores/
COPY shared/ ./shared/
COPY policy/ ./policy/

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin sidecar
USER 10001

EXPOSE 8080
CMD ["python", "server.py"]
