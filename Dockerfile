FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Root is the only user in this image, so pip's venv advice does not apply.
ENV PIP_ROOT_USER_ACTION=ignore

COPY requirements.lock .
# --require-hashes: every artifact is verified against requirements.lock, so a
# compromised index cannot swap one out. --only-binary :all: refuses sdists,
# whose setup.py runs at install time. pip carries vendored copies Trivy would
# flag, and nothing installs at runtime — so pip leaves with them.
RUN pip install --no-cache-dir --require-hashes --only-binary :all: \
        -r requirements.lock \
    && pip uninstall -y pip

COPY relay.py hyper.py watch.py sizer.py commands.py speaker.py coin_names.py chart.py tv_alerts.py sheets.py ./

# Nothing here needs root. uid 1000 owns the app dir and the cache directory
# the speech files go to.
RUN useradd --create-home --uid 1000 relay \
    && mkdir -p /home/relay/.cache/lexx-relay/tts \
    && chown -R relay:relay /app /home/relay
USER relay

CMD ["python", "relay.py"]
