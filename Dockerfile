FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LYNCEUS_DATA=/data HOME=/tmp MPLCONFIGDIR=/tmp/matplotlib OMP_NUM_THREADS=2
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr libglib2.0-0 libgomp1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN mkdir /state && chmod 1777 /state
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lynceus ./lynceus
COPY web ./web
COPY tests ./tests
USER 501:20
CMD ["python","-m","lynceus","serve"]
