#!/usr/bin/env python3
"""Quick ACCL visualizer: renders a G-meter overlay composited onto the source video."""
import sys
import os
import math
import struct
import subprocess
import tempfile
import shutil
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(__file__))
from gopro_map_overlay import extract_all_accl, _get_font

GAUGE_SIZE = 300
FPS = 30
DURATION = 60  # seconds of test clip
SMOOTH_WINDOW = 1.0  # seconds — filter out mount vibration
DEADZONE_G = 0.05  # ignore noise below this threshold


def render_gmeter(lateral_g, forward_g, down_val, gauge_size=GAUGE_SIZE):
    """Render a G-meter gauge: circle with a dot showing lateral+forward G."""
    img = Image.new('RGBA', (gauge_size, gauge_size), (0, 0, 0, 180))
    draw = ImageDraw.Draw(img)
    cx, cy = gauge_size // 2, gauge_size // 2
    radius = gauge_size // 2 - 20

    # Reference circles at 0.25G and 0.5G and 1.0G
    for g_ring in [0.25, 0.5, 1.0]:
        r = int(radius * g_ring)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                     outline=(80, 80, 80, 200), width=1)

    # Crosshair
    draw.line([cx - radius, cy, cx + radius, cy], fill=(60, 60, 60, 200), width=1)
    draw.line([cx, cy - radius, cx, cy + radius], fill=(60, 60, 60, 200), width=1)

    # G-force dot (lateral = left/right, forward = up/down on gauge)
    # Clamp to 1.5G display range
    scale = radius / 1.0  # 1G = full radius
    dot_x = cx + int(lateral_g * scale)
    dot_y = cy - int(forward_g * scale)  # negative because screen Y is inverted
    dot_x = max(20, min(gauge_size - 20, dot_x))
    dot_y = max(20, min(gauge_size - 20, dot_y))

    # Trail line from center to dot
    draw.line([cx, cy, dot_x, dot_y], fill=(0, 200, 255, 200), width=2)

    # Dot
    dr = 8
    draw.ellipse([dot_x - dr, dot_y - dr, dot_x + dr, dot_y + dr],
                 fill=(255, 50, 50), outline=(255, 255, 255), width=2)

    # Labels
    font = _get_font(16)
    font_sm = _get_font(13)
    draw.text((cx - 8, 2), "FWD", fill=(150, 150, 150), font=font_sm)
    draw.text((cx - 10, gauge_size - 18), "BRK", fill=(150, 150, 150), font=font_sm)
    draw.text((4, cy - 8), "L", fill=(150, 150, 150), font=font_sm)
    draw.text((gauge_size - 14, cy - 8), "R", fill=(150, 150, 150), font=font_sm)

    # Numeric readout
    font_val = _get_font(18)
    lat_str = f"Lat: {lateral_g:+.2f}G"
    fwd_str = f"Fwd: {forward_g:+.2f}G"
    draw.text((8, gauge_size - 55), lat_str, fill=(0, 200, 255), font=font_val)
    draw.text((8, gauge_size - 35), fwd_str, fill=(0, 200, 255), font=font_val)

    # Axis labels showing raw m/s² values
    ax_font = _get_font(12)
    draw.text((gauge_size - 90, 2),
              f"down:{down_val:.1f}", fill=(100, 100, 100), font=ax_font)

    return img


def main():
    if len(sys.argv) < 2:
        print("Usage: python accl_test.py <gopro.mp4> [output.mp4]")
        sys.exit(1)

    input_mp4 = sys.argv[1]
    output_mp4 = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(input_mp4), 'accl_vector_test.mp4')

    # Extract GPMF
    print("Extracting GPMF...", flush=True)
    gpmf_bin = tempfile.mktemp(suffix='.bin')
    subprocess.run(
        ['ffmpeg', '-y', '-i', input_mp4, '-codec', 'copy', '-map', '0:3',
         '-f', 'rawvideo', gpmf_bin], capture_output=True)
    with open(gpmf_bin, 'rb') as f:
        gpmf_data = f.read()
    os.unlink(gpmf_bin)

    samples = extract_all_accl(gpmf_data)
    print(f"  {len(samples)} ACCL samples", flush=True)
    if not samples:
        print("No ACCL data found.")
        sys.exit(1)

    # Probe video
    import json
    probe = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_streams', input_mp4], capture_output=True, text=True)
    vs = [s for s in json.loads(probe.stdout)['streams']
          if s['codec_type'] == 'video'][0]
    vid_duration = float(vs['duration'])
    test_dur = min(DURATION, vid_duration)
    accl_rate = len(samples) / vid_duration
    total_frames = int(test_dur * FPS)

    print(f"  Video: {vid_duration:.1f}s, rendering {test_dur:.0f}s "
          f"at {FPS}fps ({total_frames} frames)", flush=True)
    print(f"  ACCL rate: {accl_rate:.0f} Hz", flush=True)

    # Smooth ACCL data per-axis
    G = 9.80665
    window = max(1, int(SMOOTH_WINDOW * accl_rate))

    def smooth(values):
        out = []
        for i in range(len(values)):
            s = max(0, i - window // 2)
            e = min(len(values), i + window // 2 + 1)
            out.append(sum(values[s:e]) / (e - s))
        return out

    raw_down = [s[0] for s in samples]
    raw_lat = [s[1] for s in samples]
    raw_fwd = [s[2] for s in samples]

    sm_down = smooth(raw_down)
    sm_lat = smooth(raw_lat)
    sm_fwd = smooth(raw_fwd)

    # Render gauge frames
    frames_dir = tempfile.mkdtemp(prefix='accl_test_')
    print(f"Rendering {total_frames} gauge frames...", flush=True)

    def shape_g(raw_g):
        """Deadzone + sqrt scaling: dampen noise, compress peaks."""
        if abs(raw_g) < DEADZONE_G:
            return 0.0
        sign = 1.0 if raw_g > 0 else -1.0
        magnitude = abs(raw_g) - DEADZONE_G
        return sign * math.sqrt(magnitude)

    for idx in range(total_frames):
        t = idx / FPS
        ai = min(int(t * accl_rate), len(samples) - 1)
        lateral_g = shape_g(sm_lat[ai] / G)
        forward_g = shape_g(sm_fwd[ai] / G)
        down_val = sm_down[ai]

        frame = render_gmeter(lateral_g, forward_g, down_val)
        frame.save(os.path.join(frames_dir, f"frame_{idx:06d}.png"))

        if (idx + 1) % 100 == 0:
            print(f"  {idx + 1}/{total_frames}", flush=True)

    # Composite gauge onto source video (bottom-left corner)
    print("Compositing...", flush=True)
    input_pattern = os.path.join(frames_dir, 'frame_%06d.png')
    margin = 20
    filter_str = (
        f"[0:v]scale=1920:1080[base];"
        f"[1:v]fps={FPS}[gauge];"
        f"[base][gauge]overlay={margin}:{1080 - GAUGE_SIZE - margin}:shortest=1"
    )
    cmd = [
        'ffmpeg', '-y',
        '-ss', '0', '-t', str(test_dur), '-i', input_mp4,
        '-framerate', str(FPS), '-i', input_pattern,
        '-filter_complex', filter_str,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-pix_fmt', 'yuv420p', '-an', output_mp4
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        print(f"Encode error: {result.stderr[-500:]}", flush=True)
    else:
        size_mb = os.path.getsize(output_mp4) / (1024 * 1024)
        print(f"\nDone! {output_mp4} ({size_mb:.1f} MB)", flush=True)

    shutil.rmtree(frames_dir)


if __name__ == '__main__':
    main()
