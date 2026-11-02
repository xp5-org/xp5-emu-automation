# syntax=docker/dockerfile:1
# GNU Radio for the LoRa / Meshtastic / MeshCore SDR flowgraphs. gr-lora_sdr and gr-osmosdr are built into a conda env
# (/opt/conda/envs/gr310) on top of the conda GNU Radio; Ubuntu's own gnuradio
# (pkgs/gnuradio.apt) has no lora_sdr blocks. The env is NOT put on PATH, so it
# cannot shadow /opt/venv: gnuradio-companion and grcc get wrappers in
# /usr/local/bin that activate gr310, and flowgraph scripts run with
# /opt/conda/envs/gr310/bin/python. Decoder deps (pyzmq, meshtastic,
# meshcoredecoder, ...) go into the shared /opt/venv. Not included: the
# Meshtastic firmware checkout and platformio.
FROM base AS build
ARG APT_PROXY=
SHELL ["/bin/bash", "-c"]
RUN --mount=type=bind,source=docker,target=/docker \
    sh /docker/apt-install.sh /docker/pkgs/gnuradio.apt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /tmp
RUN set -eux; \
    touch /tmp/stamp; \
    mkdir -p /data; \
    # miniconda + the gr310 env from gr-lora_sdr's environment.yml
    git clone --depth 1 https://github.com/tapparelj/gr-lora_sdr.git /data/gr-lora_sdr; \
    git clone --depth 1 https://gitlab.com/crankylinuxuser/meshtastic_sdr /data/meshtastic_sdr; \
    git clone --depth 1 https://git.osmocom.org/gr-osmosdr /data/gr-osmosdr; \
    wget -q -O /tmp/miniconda.sh https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh && \
    bash /tmp/miniconda.sh -b -p /opt/conda && rm /tmp/miniconda.sh; \
    /opt/conda/bin/conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main; \
    /opt/conda/bin/conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r; \
    /opt/conda/bin/conda env create -f /data/gr-lora_sdr/environment.yml -p /opt/conda/envs/gr310 -y; \
    /opt/conda/envs/gr310/bin/pip install pillow six; \
    source /opt/conda/etc/profile.d/conda.sh; \
    conda activate gr310; \
    # gr-lora_sdr
    cmake -S /data/gr-lora_sdr -B /data/gr-lora_sdr/build -DCMAKE_INSTALL_PREFIX=$CONDA_PREFIX && \
    make -C /data/gr-lora_sdr/build install -j"$(nproc)"; \
    # gr-osmosdr, RTL-SDR only
    CC=/usr/bin/gcc CXX=/usr/bin/g++ PKG_CONFIG_PATH=/usr/lib/x86_64-linux-gnu/pkgconfig:${PKG_CONFIG_PATH:-} \
    cmake -S /data/gr-osmosdr -B /data/gr-osmosdr/build \
        -DCMAKE_INSTALL_PREFIX=$CONDA_PREFIX -DPYTHON_EXECUTABLE=$CONDA_PREFIX/bin/python3 \
        -DENABLE_DEFAULT=OFF -DENABLE_RTL=ON \
        -DLIBRTLSDR_INCLUDE_DIR=/usr/include \
        -DLIBRTLSDR_LIBRARIES=/usr/lib/x86_64-linux-gnu/librtlsdr.so \
        -DCMAKE_CXX_FLAGS="-I/usr/include/libusb-1.0" && \
    make -C /data/gr-osmosdr/build install -j"$(( ($(nproc) / 2) > 0 ? $(nproc) / 2 : 1 ))"; \
    rm -rf /data/gr-lora_sdr/build /data/gr-osmosdr/build; \
    conda clean -afy; \
    # decoder deps into the shared venv
    /opt/venv/bin/pip install --no-cache-dir pyzmq meshtastic cryptography scipy matplotlib "meshcoredecoder>=0.3.2"; \
    # GRC defaults to gr310 (the only env with the lora_sdr / gr-iio blocks)
    for tool in gnuradio-companion grcc; do \
        printf '%s\n' '#!/bin/bash' \
            'source /opt/conda/etc/profile.d/conda.sh' \
            'conda activate gr310' \
            "exec /opt/conda/envs/gr310/bin/$tool \"\$@\"" \
            > "/usr/local/bin/$tool" && chmod 755 "/usr/local/bin/$tool"; \
    done; \
    # world/group-writable so the non-root user can add packages
    chgrp -R users /opt/conda/envs /data /opt/venv && \
    chmod -R g+rwX /opt/conda/envs /data /opt/venv; \
    mkdir /out && \
    cp -a --parents /opt/conda /data/gr-lora_sdr /data/meshtastic_sdr /data/gr-osmosdr /out && \
    find /usr/local/bin /opt/venv -newer /tmp/stamp \( -type f -o -type l \) -print0 | \
        xargs -0 cp -a --parents -t /out

FROM scratch
COPY --from=build /out/ /
