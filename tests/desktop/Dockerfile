FROM ubuntu:24.04 AS desktop-base

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates python3 python3-venv python3-gi gir1.2-gtk-3.0 \
    xvfb xauth xdotool x11-utils openbox dbus dbus-x11 at-spi2-core \
    libglib2.0-bin libatk-adaptor mousepad thunar evince poppler-utils fonts-dejavu-core \
    blender inkscape gimp libreoffice-writer libreoffice-calc libreoffice-impress \
    libreoffice-gtk3 fonts-liberation fonts-noto-core mesa-utils libgl1-mesa-dri \
    && rm -rf /var/lib/apt/lists/*

FROM desktop-base AS desktop-test
WORKDIR /opt/meerkat-computer
COPY pyproject.toml uv.lock ./
RUN python3 -m venv --system-site-packages /opt/venv \
    && pip install --no-cache-dir uv==0.10.9 \
    && uv sync --locked --no-dev --no-install-project \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

RUN apt-get update && apt-get install -y --no-install-recommends libgail-common \
    && rm -rf /var/lib/apt/lists/*
COPY python ./python
COPY README.md LICENSE ./
COPY tests/desktop ./tests/desktop
COPY scripts/desktop_smoke.py ./scripts/desktop_smoke.py
COPY infra/desktop-session.py ./infra/desktop-session.py
RUN chmod -R a+rX python tests scripts infra \
    && uv pip install --no-deps --python /opt/venv/bin/python -e . \
    && useradd --create-home --uid 10001 computer \
    && mkdir /tmp/desktop-evidence \
    && chown computer:computer /tmp/desktop-evidence

USER computer
ENV HOME=/home/computer
ENTRYPOINT ["python", "infra/desktop-session.py", "--width", "1280", "--height", "720", "--"]
CMD ["python", "scripts/desktop_smoke.py", "--evidence", "/tmp/desktop-evidence"]
