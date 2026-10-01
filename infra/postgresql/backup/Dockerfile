# 운영과 동일한 PostgreSQL 17.11 / pgvector 0.8.6 기반. 운영 게시에는 새 이미지 digest를 사용한다.
FROM pgvector/pgvector@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f
RUN apt-get update \
    && apt-get install -y --no-install-recommends age=1.1.1-1+b3 python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/backup-venv
COPY requirements.txt /opt/backup/requirements.txt
RUN /opt/backup-venv/bin/pip install --no-cache-dir -r /opt/backup/requirements.txt
COPY backup.py restore.py download.py drill.py /opt/backup/
COPY tests /opt/backup/tests
ENV PATH="/opt/backup-venv/bin:${PATH}" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN mkdir /work && chown postgres:postgres /work
USER postgres
WORKDIR /opt/backup
ENTRYPOINT ["python3", "/opt/backup/backup.py"]
