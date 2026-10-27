# Optional preinstalled executor; the default Job reuses the node image.
# Build from the repository root; install the published SDK, including RCs.
FROM python:3.12-slim
WORKDIR /opt/akernel
COPY deploy/upgrade/requirements.txt /tmp/upgrade-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/upgrade-requirements.txt \
    && python -m pip install --no-cache-dir --upgrade --pre akernel-sdk
COPY deploy/upgrade /opt/akernel/upgrade
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin upgrade
USER 10001:10001
ENTRYPOINT ["python", "/opt/akernel/upgrade/manage.py"]
CMD ["plan", "--config", "/etc/akernel-upgrade/config.json"]
