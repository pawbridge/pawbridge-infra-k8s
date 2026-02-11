FROM confluentinc/cp-kafka-connect:7.5.0

# Install Debezium MySQL Connector
RUN confluent-hub install --no-prompt debezium/debezium-connector-mysql:2.2.1

# Install Elasticsearch Sink Connector
RUN confluent-hub install --no-prompt confluentinc/kafka-connect-elasticsearch:latest
