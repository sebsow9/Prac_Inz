"""H.264/RTP through local UDP; FFmpeg handles RTP on both sides."""
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
from contextlib import contextmanager


def validate_config(transport, packet_size):
    if transport not in ("raw", "rtp"):
        raise ValueError("TRANSPORT must be raw or rtp")
    if transport == "rtp" and not 256 <= packet_size <= 1400:
        raise ValueError("RTP_PACKET_B must be between 256 and 1400")


def validate_rtp(packet, maximum=1400):
    if not 12 <= len(packet) <= maximum or packet[0] >> 6 != 2:
        raise ValueError("Invalid RTP packet length or version")
    if packet[1] & 127 != 96:
        raise ValueError("Expected H.264 RTP payload type 96")
    header = 12 + 4 * (packet[0] & 15)
    if len(packet) < header:
        raise ValueError("Truncated RTP CSRC list")
    if packet[0] & 16:
        if len(packet) < header + 4:
            raise ValueError("Truncated RTP extension")
        words = struct.unpack("!H", packet[header + 2:header + 4])[0]
        header += 4 + words * 4
    padding = packet[-1] if packet[0] & 32 else 0
    if packet[0] & 32 and not padding:
        raise ValueError("Invalid RTP padding")
    if header >= len(packet) - padding:
        raise ValueError("Missing RTP payload")


def raw_chunks(stream, size):
    while True:
        data = stream.read(size)
        if not data:
            return
        yield data


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@contextmanager
def video_source(ffmpeg, transport, packet_size, chunk_size, log_path):
    local = process = None
    with open(log_path, "wb") as errors:
        try:
            if transport == "rtp":
                local = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                local.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
                local.bind(("127.0.0.1", 0))
                local.settimeout(0.1)
                port = local.getsockname()[1]
                command = ffmpeg + ["-f", "rtp", "-payload_type", "96",
                    "-rtpflags", "skip_rtcp",
                    f"rtp://127.0.0.1:{port}?pkt_size={packet_size}"]
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors)

                def packets():
                    while True:
                        try:
                            packet, _ = local.recvfrom(65535)
                        except socket.timeout:
                            if process.poll() is not None:
                                return  # Queue has drained after FFmpeg exited.
                            continue
                        validate_rtp(packet, packet_size)
                        yield packet
                yield packets()
            else:
                process = subprocess.Popen(ffmpeg + ["-f", "h264", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=errors)
                yield raw_chunks(process.stdout, chunk_size)
            if process.wait(timeout=30):
                raise RuntimeError(f"FFmpeg encoder failed; see {log_path}")
        finally:
            if process:
                _stop(process)
                if process.stdout:
                    process.stdout.close()
            if local:
                local.close()


def _reserve_ports():
    """Reserve an even RTP port and its adjacent RTCP port until launch."""
    for _ in range(100):
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp.bind(("127.0.0.1", 0))
        port = rtp.getsockname()[1]
        rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if port % 2:
            rtp.close()
            rtcp.close()
            continue
        try:
            rtcp.bind(("127.0.0.1", port + 1))
        except OSError:
            rtp.close()
            rtcp.close()
            continue
        return port, (rtp, rtcp)
    raise RuntimeError("Cannot reserve local RTP/RTCP ports")


def _udp_queue(port):
    """Linux containers: check socket startup/drain without a fixed sleep."""
    for line in Path("/proc/net/udp").read_text().splitlines()[1:]:
        fields = line.split()
        if int(fields[1].split(":")[1], 16) == port:
            return int(fields[4].split(":")[1], 16)
    return None


@contextmanager
def video_sink(transport, output, fps, log_path):
    process = local = None
    reserved = ()
    with tempfile.TemporaryDirectory(prefix="h264_rtp_") as directory, open(log_path, "wb") as errors:
        try:
            if transport == "rtp":
                port, reserved = _reserve_ports()
                sdp = Path(directory) / "stream.sdp"
                sdp.write_text(
                    "v=0\no=- 0 0 IN IP4 127.0.0.1\ns=H264 experiment\n"
                    f"c=IN IP4 127.0.0.1\nt=0 0\nm=video {port} RTP/AVP 96\n"
                    "b=RS:0\nb=RR:0\na=rtpmap:96 H264/90000\n"
                    "a=fmtp:96 packetization-mode=1\n", encoding="ascii")
                for sock in reserved:
                    sock.close()
                command = ["ffmpeg", "-nostdin", "-y", "-loglevel", "warning",
                    "-protocol_whitelist", "file,udp,rtp", "-fflags", "+genpts",
                    "-buffer_size", str(4 << 20), "-reorder_queue_size", "0",
                    "-max_delay", "0", "-listen_timeout", "2",
                    "-analyzeduration", "100000", "-fpsprobesize", "0", "-r", fps, "-i", str(sdp),
                    "-c", "copy", output]
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stderr=errors)
                deadline = time.monotonic() + 10
                while _udp_queue(port) is None:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError(f"FFmpeg did not open local RTP port; see {log_path}")
                    time.sleep(0.01)
                local = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                ssrc = None

                def deliver(packet):
                    nonlocal ssrc
                    ssrc = packet[8:12]
                    local.sendto(packet, ("127.0.0.1", port))
                yield deliver
                # FIN concerns the encrypted tunnel. Terminate the local RTP
                # session separately, after queued video datagrams are read.
                deadline = time.monotonic() + 10
                while _udp_queue(port) not in (None, 0) and process.poll() is None:
                    if time.monotonic() > deadline:
                        raise RuntimeError("Local FFmpeg RTP queue did not drain")
                    time.sleep(0.01)
                if ssrc:
                    local.sendto(b"\x81\xcb\x00\x02" + ssrc + b"\x03FIN", ("127.0.0.1", port + 1))
            else:
                process = subprocess.Popen(
                    ["ffmpeg", "-nostdin", "-y", "-r", fps, "-f", "h264",
                     "-i", "pipe:0", "-c", "copy", output],
                    stdin=subprocess.PIPE, stderr=errors)
                yield process.stdin.write
                process.stdin.close()
            if process.wait(timeout=30):
                raise RuntimeError(f"FFmpeg receiver failed; see {log_path}")
        finally:
            if process:
                _stop(process)
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
            if local:
                local.close()
            for sock in reserved:
                sock.close()
