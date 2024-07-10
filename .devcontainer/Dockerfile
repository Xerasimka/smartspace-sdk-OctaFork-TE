FROM python:3.10-buster

WORKDIR /src

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=dev

# Combine requirements copy to optimize layer caching
COPY pyproject.toml poetry.lock* ./

# Install Poetry and disable virtualenv creation
RUN pip install --upgrade pip && \
    pip install poetry==1.8.2 && \
    poetry config virtualenvs.create false

# Install project dependencies via Poetry
RUN poetry install

# System dependencies
RUN apt-get update && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY ./smartspace ./smartspace
COPY .vscode/ .vscode/

CMD ["python"]
