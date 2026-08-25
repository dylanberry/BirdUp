#!/usr/bin/env python3
"""Bird Up! mic-node RTSP relay: loopback PCM tee -> continuous PCM on stdout.

Receives decoded 24 kHz mono s16le PCM from birdnet-dumpd's live tee
(UDP 127.0.0.1:8556, fire-and-forget) and emits it on stdout, padding with
digital silence whenever the mic node is between dumps. The downstream
ffmpeg (spawned by mediamtx runOnInit via mic-relay.sh) therefore always has
frames to encode, so the RTSP path /mic stays published continuously —
mediamtx paths only exist while a publisher is attached, and the cluster
BirdNET-Go's probe needs the path up even during silent gaps.

History: this used to join the birdup-fanout multicast group (224.0.0.100),
but the node firmware now only dumps over TCP (:8557, ima-adpcm), so nothing
ever arrived on :8555 and the RTSP stream was pure silence. The dumpd tee is
the live source now. PCM zeros = silence; mic PCM passes through verbatim
(no resample — ffmpeg resamples 24 kHz -> 48 kHz for the AAC/RTSP stream).
"""

import socket
import sys

LISTEN = ("127.0.0.1", 8556)
SILENCE = b"\x00\x00" * 2400  # 100 ms of 24 kHz mono s16le silence

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
# Dumps arrive as fast-as-decode bursts (minutes of audio in seconds);
# a deep receive buffer keeps loopback drops low while ffmpeg drains us.
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
s.bind(LISTEN)
s.settimeout(0.2)

out = sys.stdout.buffer
while True:
    try:
        data, _ = s.recvfrom(4096)
        if data:
            out.write(data)
            out.flush()
    except socket.timeout:
        out.write(SILENCE)
        out.flush()
