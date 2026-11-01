# syntax=docker/dockerfile:1
# Web automation: Firefox + geckodriver, Chromium (Chrome for Testing build,
# since Ubuntu's chromium package is a snap) + chromedriver, and the selenium
# Python package in the shared /opt/venv. Browsers go to /opt, with symlinks in
# /usr/local/bin. Runtime libs are in pkgs/webtools.apt (final image).
FROM base AS build
ARG APT_PROXY=
RUN --mount=type=bind,source=docker,target=/docker \
    /docker/apt-install.sh /docker/pkgs/webtools.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
RUN set -eux; \
    touch /tmp/stamp; \
    # Firefox (latest)
    curl -fsSL -o /tmp/firefox.tar.xz "https://download.mozilla.org/?product=firefox-latest&os=linux64&lang=en-US" && \
    tar -xf /tmp/firefox.tar.xz -C /opt && \
    ln -s /opt/firefox/firefox /usr/local/bin/firefox && \
    rm /tmp/firefox.tar.xz; \
    # geckodriver (latest)
    GECKO_URL=$(python3 -c "import urllib.request, json; r = json.load(urllib.request.urlopen('https://api.github.com/repos/mozilla/geckodriver/releases/latest')); print([a['browser_download_url'] for a in r['assets'] if a['name'].endswith('linux64.tar.gz')][0])") && \
    curl -fsSL "$GECKO_URL" | tar -xz -C /usr/local/bin geckodriver; \
    # Chromium + chromedriver (Chrome for Testing, stable)
    python3 -c "import urllib.request, json; d = json.load(urllib.request.urlopen('https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json'))['channels']['Stable']['downloads']; [print(k, [x['url'] for x in d[k] if x['platform']=='linux64'][0]) for k in ('chrome','chromedriver')]" > /tmp/cft.txt && \
    while read -r name url; do \
        curl -fsSL -o /tmp/$name.zip "$url" && unzip -q /tmp/$name.zip -d /opt && rm /tmp/$name.zip; \
    done < /tmp/cft.txt && \
    ln -s /opt/chrome-linux64/chrome /usr/local/bin/chromium && \
    ln -s /opt/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver; \
    # selenium into the base image's venv
    /opt/venv/bin/pip install selenium; \
    mkdir /out && \
    find /usr/local /opt/firefox /opt/chrome-linux64 /opt/chromedriver-linux64 /opt/venv \
        -newer /tmp/stamp \( -type f -o -type l \) -print0 | \
        xargs -0 cp -a --parents -t /out

FROM scratch
COPY --from=build /out/ /
