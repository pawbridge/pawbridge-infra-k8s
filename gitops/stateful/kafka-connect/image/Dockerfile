# syntax=docker/dockerfile:1.7

ARG ALPINE_IMAGE=docker.io/library/alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1
ARG STRIMZI_IMAGE=quay.io/strimzi/kafka@sha256:39bce6f044fe43c62b1355b4e855866899685f316db15cd25a27f7f37fffa99f

FROM ${ALPINE_IMAGE} AS plugin-downloader

ARG DEBEZIUM_VERSION=3.5.2.Final
ARG DEBEZIUM_ARCHIVE_SHA512=8b125b8be1cf481a325ea02d3f73f81c238e5429e4d4c5d733cbf773e194f4a262c3c1794f18b04229dee1790f8cc306c1f5d7a2d29dd48d865ca70722e9ba26
ARG ELASTICSEARCH_CONNECTOR_VERSION=15.1.3
ARG ELASTICSEARCH_ARCHIVE_SHA512=4ee44f0e5194437275e5bc0a36d0959807e6669ce6bcc020817d8bdf30ee98782e9b716098425ca8239031f8bb8d2e3332a235721af34fdd63d494d349fa72e1

RUN apk add --no-cache ca-certificates curl tar unzip

RUN set -eux; \
    debezium_archive="/tmp/debezium-mysql.tar.gz"; \
    elasticsearch_archive="/tmp/kafka-connect-elasticsearch.zip"; \
    curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
      --retry 5 --retry-all-errors --connect-timeout 15 --max-time 300 \
      --output "${debezium_archive}" \
      "https://repo.maven.apache.org/maven2/io/debezium/debezium-connector-mysql/${DEBEZIUM_VERSION}/debezium-connector-mysql-${DEBEZIUM_VERSION}-plugin.tar.gz"; \
    echo "${DEBEZIUM_ARCHIVE_SHA512}  ${debezium_archive}" | sha512sum -c -; \
    curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
      --retry 5 --retry-all-errors --connect-timeout 15 --max-time 300 \
      --output "${elasticsearch_archive}" \
      "https://api.hub.confluent.io/api/plugins/confluentinc/kafka-connect-elasticsearch/versions/${ELASTICSEARCH_CONNECTOR_VERSION}/archive"; \
    echo "${ELASTICSEARCH_ARCHIVE_SHA512}  ${elasticsearch_archive}" | sha512sum -c -; \
    mkdir -p /plugins/debezium-mysql /plugins/elasticsearch /tmp/elasticsearch; \
    tar --extract --gzip --file "${debezium_archive}" \
      --directory /plugins/debezium-mysql --strip-components=1; \
    unzip -q "${elasticsearch_archive}" -d /tmp/elasticsearch; \
    cp -a \
      "/tmp/elasticsearch/confluentinc-kafka-connect-elasticsearch-${ELASTICSEARCH_CONNECTOR_VERSION}/lib/." \
      /plugins/elasticsearch/

FROM ${STRIMZI_IMAGE}

USER root
COPY --from=plugin-downloader --chown=1001:0 /plugins/ /opt/kafka/plugins/
USER 1001

RUN test -r /opt/kafka/plugins/debezium-mysql/debezium-connector-mysql-3.5.2.Final.jar \
    && test -r /opt/kafka/plugins/elasticsearch/kafka-connect-elasticsearch-15.1.3.jar
