FROM python:3.14

WORKDIR /app

COPY uv.lock .
RUN python -m pip install --no-cache-dir --upgrade pip wheel setuptools uv
COPY pyproject.toml .
RUN uv sync

COPY . .
RUN make protobufs
