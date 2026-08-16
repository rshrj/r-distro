ARG BASE_IMAGE=debian:testing
FROM ${BASE_IMAGE}

RUN rm -f /etc/apt/sources.list \
          /etc/apt/sources.list.d/debian.sources && \
    cat > /etc/apt/sources.list.d/debian.sources <<'EOF'
Types: deb deb-src
URIs: http://snapshot.debian.org/archive/debian/20260813T165000Z/
Suites: forky
Components: main
Check-Valid-Until: no
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        devscripts \
        dpkg-dev \
        fakeroot \
        equivs \
        passwd \
        util-linux && \
    rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash builder

WORKDIR /build