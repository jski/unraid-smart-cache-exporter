FROM ghcr.io/jski/python-container-builder:3.12 AS build-venv

WORKDIR /app
COPY exporter.py /app/exporter.py

FROM gcr.io/distroless/python3-debian12:nonroot

WORKDIR /app
COPY --from=build-venv /usr/local /usr/local
COPY --from=build-venv /.venv /.venv
COPY --from=build-venv /app/exporter.py /app/exporter.py

EXPOSE 9903
ENTRYPOINT ["/.venv/bin/python3", "-u", "/app/exporter.py"]
