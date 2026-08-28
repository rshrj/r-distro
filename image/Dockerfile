FROM rdistro-buildroot:2026-08-13

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        simple-cdd \
        debian-cd \
        reprepro \
        rsync \
        wget \
        xorriso \
        dosfstools \
        mtools \
        ca-certificates \
        gnupg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /work