#!/bin/sh
# Bird Up! mic RTSP relay pipeline (single executable for mediamtx runOnInit,
# which does not run commands through a shell — the pipe lives here instead).
# rtsp-relay.py emits continuous 24 kHz mono s16le PCM (birdnet-dumpd's live
# tee, silence-padded between dumps); ffmpeg resamples to 48 kHz, encodes AAC
# and publishes RTSP to mediamtx's /mic path for the cluster BirdNET-Go.
exec python3 /usr/local/bin/rtsp-relay.py | ffmpeg -hide_banner -loglevel error -nostdin -f s16le -ar 24000 -ac 1 -i pipe:0 -af aresample=48000 -c:a aac -b:a 96k -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/mic
