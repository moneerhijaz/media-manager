FROM python:3.12-slim

ARG INSTALL_AVSYNC=1

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp/media-manager-home

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-avsync.txt /app/
RUN pip install -r requirements.txt \
    && if [ "$INSTALL_AVSYNC" = "1" ]; then pip install -r requirements-avsync.txt; fi \
    && mkdir -p "$HOME"

COPY . /app
RUN pip install . --no-deps

COPY docker-entrypoint.sh /usr/local/bin/media-manager-entrypoint
RUN chmod 0755 /usr/local/bin/media-manager-entrypoint

EXPOSE 8787
VOLUME ["/library"]
ENTRYPOINT ["media-manager-entrypoint"]
CMD ["media-manager", "web", "--library-root", "/library", "--host", "0.0.0.0", "--port", "8787"]
