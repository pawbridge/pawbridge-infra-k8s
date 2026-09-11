# Build context: ONE service's build/migration directory, never the whole repository.
# Official Eclipse Temurin Java 17 JRE, Ubuntu Jammy, linux/amd64.
# Update this digest together with the five-service package drill evidence.
FROM eclipse-temurin:17-jre-jammy@sha256:24cd8eed18b5976441d27b45823490eb5e8efff4b3ecdc632e442717ea66f160
WORKDIR /opt/migration
COPY --chown=1000:1000 lib/ ./lib/
USER 1000:1000
# The Job supplies the service-owned class and command explicitly.
ENTRYPOINT ["java"]
