FROM debian@sha256:8fe7e20ebb14e121fa7abd75c2d8de93f2fc2e9ce1f92748f10257b29350d276

RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources &&     cat > /etc/apt/sources.list.d/debian.sources <<'SOURCES'
Types: deb deb-src
URIs: http://snapshot.debian.org/archive/debian/20260813T165000Z/
Suites: forky
Components: main
Check-Valid-Until: no
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
SOURCES

RUN apt-get update &&     apt-get install -y --no-install-recommends         sbuild         autopkgtest         docker-cli         devscripts         dpkg-dev         ca-certificates &&     rm -rf /var/lib/apt/lists/*

WORKDIR /work
