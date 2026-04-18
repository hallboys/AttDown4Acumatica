FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip \
 && pip install ".[all]"

COPY examples ./examples
COPY endpoint-extensions ./endpoint-extensions

VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["attdown"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
