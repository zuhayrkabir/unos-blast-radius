# Same base image the commits were labeled in. Nothing is downloaded: the pinned sqlglot
# sources (eb11aec, .py files only) ship in sqlglot_src.tar.gz and are only read, never run.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY selector.py map.json.gz sqlglot_src.tar.gz /app/
RUN python3 -c "import tarfile; tarfile.open('/app/sqlglot_src.tar.gz').extractall('/app', filter='data')" \
    && rm /app/sqlglot_src.tar.gz && chmod -R a+rX /app
CMD ["python3", "/app/selector.py"]
