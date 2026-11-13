FROM apache/airflow:3.4.0-python3.12
COPY --chown=airflow:root pyproject.toml /opt/accounting/pyproject.toml
COPY --chown=airflow:root src /opt/accounting/src
RUN pip install --no-cache-dir --no-deps /opt/accounting
