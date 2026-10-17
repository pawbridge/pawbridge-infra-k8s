# Mac synthetic rehearsal only. The parent selects one actual Spring Boot bootJar.
# Immutable ARM64 child of the reviewed Eclipse Temurin 17 manifest index.
FROM docker.io/library/eclipse-temurin@sha256:dfcfafa8207eb07cefd1e67e831d763cf2a5c8994ff64594c6dd95809fa52263

# The build context contains only the selected bootJar, normally renamed app.jar.
# Never pass build/libs/*.jar: plain and executable JARs are different artifacts.
ARG BOOT_JAR=app.jar
ARG SOURCE_REVISION=501d513b76a7340c6a03fcb34ebb55f0d1984650
LABEL org.opencontainers.image.source="https://github.com/pawbridge/pawbridge-backend-k8s" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      com.pawbridge.purpose="synthetic-gateway-user-animal"

COPY --chown=10001:10001 ["${BOOT_JAR}", "/app.jar"]
USER 10001:10001
WORKDIR /
ENTRYPOINT ["java", "-jar", "/app.jar"]
