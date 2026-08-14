FROM debian:testing

RUN cat > /etc/apt/sources.list.d/debian.sources <<'EOF'
Types: deb deb-src
URIs: http://snapshot.debian.org/archive/debian/20260813T165000Z/
Suites: forky
Components: main
Check-Valid-Until: no
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        devscripts \
        dpkg-dev \
        fakeroot \
        apt-utils \
        gnupg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build
