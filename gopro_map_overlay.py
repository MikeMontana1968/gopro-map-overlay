#!/usr/bin/env python3
"""
gopro-map-overlay: Generate a GPS map overlay video from GoPro MP4 files.

Extracts the GPS telemetry (GPMF) embedded in GoPro videos, renders a moving
map with trail, current position, timestamp, and ground speed, and outputs a
small overlay video that can be layered onto the original footage in any editor.
"""
import argparse
import struct
import subprocess
import sys
import os
import math
import tempfile
import shutil
import json
from io import BytesIO
from urllib.request import urlopen, Request
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont, ImageFilter


# ---------------------------------------------------------------------------
# GPMF binary parser
# ---------------------------------------------------------------------------

def parse_gpmf(data, start, end):
    """Recursively parse GPMF KLV entries from binary data."""
    entries = []
    pos = start
    while pos + 8 <= end:
        fourcc = data[pos:pos+4].decode('ascii', errors='replace')
        type_byte = data[pos+4]
        ss = data[pos+5]
        repeat = struct.unpack('>H', data[pos+6:pos+8])[0]
        total = ss * repeat
        padded = (total + 3) & ~3
        if type_byte == 0:
            children = parse_gpmf(data, pos+8, min(pos+8+padded, end))
            entries.append((fourcc, 'container', children))
        else:
            raw = data[pos+8:pos+8+total]
            entries.append((fourcc, chr(type_byte), (ss, repeat, raw)))
        pos += 8 + padded
    return entries


def extract_all_gps(data):
    """Walk all DEVC containers in the GPMF stream and collect GPS points."""
    points = []
    pos = 0
    while pos + 8 <= len(data):
        fourcc = data[pos:pos+4].decode('ascii', errors='replace')
        type_byte = data[pos+4]
        ss = data[pos+5]
        repeat = struct.unpack('>H', data[pos+6:pos+8])[0]
        total = ss * repeat
        padded = (total + 3) & ~3
        if fourcc == 'DEVC' and type_byte == 0:
            children = parse_gpmf(data, pos+8, pos+8+padded)
            for fc, typ, val in children:
                if fc == 'STRM' and typ == 'container':
                    if any(cc in ('GPS5', 'GPS9') for cc, _, _ in val):
                        points.extend(_extract_gps_from_strm(val))
        pos += 8 + padded
    return points


def _extract_gps_from_strm(children):
    """Extract GPS5/GPS9 samples from a single STRM, applying SCAL divisors."""
    scale = None
    gps_fix = 0
    points = []
    for fourcc, typ, val in children:
        if fourcc == 'GPSF':
            _, _, raw = val
            gps_fix = struct.unpack('>I', raw[:4])[0]
        if fourcc == 'SCAL' and typ == 'l':
            _, _, raw = val
            scale = [struct.unpack('>i', raw[i*4:(i+1)*4])[0]
                     for i in range(len(raw) // 4)]
        if fourcc in ('GPS5', 'GPS9') and typ == 'l':
            ss, repeat, raw = val
            if scale is None:
                continue
            fields = ss // 4
            for i in range(repeat):
                offset = i * ss
                values = []
                for j in range(fields):
                    v = struct.unpack('>i', raw[offset+j*4:offset+j*4+4])[0]
                    if j < len(scale) and scale[j] != 0:
                        v = v / scale[j]
                    values.append(v)
                if len(values) >= 2 and gps_fix >= 2:
                    points.append({
                        'lat': values[0],
                        'lon': values[1],
                        'alt': values[2] if len(values) > 2 else 0,
                        'speed': values[3] if len(values) > 3 else 0,
                    })
    return points


# ---------------------------------------------------------------------------
# Accelerometer (ACCL) extraction
# ---------------------------------------------------------------------------

def _extract_accl_from_strm(children):
    """Extract ACCL accelerometer samples from a single STRM."""
    scale = None
    samples = []
    for fourcc, typ, val in children:
        if fourcc == 'SCAL':
            ss_s, _, raw_s = val
            if typ == 'l':
                scale = [struct.unpack('>i', raw_s[i*4:(i+1)*4])[0]
                         for i in range(len(raw_s) // 4)]
            elif typ == 's':
                scale = [struct.unpack('>h', raw_s[i*2:(i+1)*2])[0]
                         for i in range(len(raw_s) // 2)]
        if fourcc == 'ACCL':
            ss, repeat, raw = val
            if typ == 's':
                fields = ss // 2
                for i in range(repeat):
                    offset = i * ss
                    values = []
                    for j in range(fields):
                        v = struct.unpack('>h', raw[offset+j*2:offset+j*2+2])[0]
                        if scale:
                            s = scale[j] if j < len(scale) else scale[-1]
                            if s != 0:
                                v = v / s
                        values.append(v)
                    if len(values) >= 3:
                        samples.append((values[0], values[1], values[2]))
    return samples


def extract_all_accl(data):
    """Walk all DEVC containers and collect accelerometer samples."""
    samples = []
    pos = 0
    while pos + 8 <= len(data):
        fourcc = data[pos:pos+4].decode('ascii', errors='replace')
        type_byte = data[pos+4]
        ss = data[pos+5]
        repeat = struct.unpack('>H', data[pos+6:pos+8])[0]
        total = ss * repeat
        padded = (total + 3) & ~3
        if fourcc == 'DEVC' and type_byte == 0:
            children = parse_gpmf(data, pos+8, pos+8+padded)
            for fc, typ, val in children:
                if fc == 'STRM' and typ == 'container':
                    if any(cc == 'ACCL' for cc, _, _ in val):
                        samples.extend(_extract_accl_from_strm(val))
        pos += 8 + padded
    return samples


def compute_pitch_angles(accl_samples, video_duration, video_fps,
                         exaggeration=3.0, smooth_window_sec=1.5,
                         max_degrees=15.0):
    """Compute per-frame roll angles from accelerometer data.

    Returns list of angles in radians, one per video frame.
    GoPro ACCL axes (camera upright, lens forward): [Y-down, X-right, Z-forward].
    Roll = atan2(X, -Y) measures lateral tilt from cornering forces.
    The accelerometer naturally combines centripetal + road camber.
    Angles are clamped to ±max_degrees after exaggeration.
    """
    if not accl_samples:
        return []

    accl_rate = len(accl_samples) / video_duration

    raw_pitch = []
    for y, x, z in accl_samples:
        raw_pitch.append(math.atan2(x, -y))

    median_pitch = sorted(raw_pitch)[len(raw_pitch) // 2]
    raw_pitch = [p - median_pitch for p in raw_pitch]

    # Handle angular wrapping: clamp raw deviations to ±π/2
    half_pi = math.pi / 2
    raw_pitch = [max(-half_pi, min(half_pi, p)) for p in raw_pitch]

    window = max(1, int(smooth_window_sec * accl_rate))
    smoothed = []
    for i in range(len(raw_pitch)):
        start = max(0, i - window // 2)
        end = min(len(raw_pitch), i + window // 2 + 1)
        smoothed.append(sum(raw_pitch[start:end]) / (end - start))

    max_rad = math.radians(max_degrees)
    total_frames = int(video_duration * video_fps)
    angles = []
    for frame in range(total_frames):
        t = frame / video_fps
        accl_idx = min(int(t * accl_rate), len(smoothed) - 1)
        a = smoothed[accl_idx] * exaggeration
        angles.append(max(-max_rad, min(max_rad, a)))

    return angles


def _write_pitch_cmdfile(path, angles, video_fps):
    """Write ffmpeg sendcmd file for per-frame pitch rotation."""
    with open(path, 'w') as f:
        for i, angle in enumerate(angles):
            t = i / video_fps
            f.write(f"{t:.4f} rotate a {angle:.6f};\n")


# ---------------------------------------------------------------------------
# Map tile rendering
# ---------------------------------------------------------------------------

TILE_SERVERS = {
    'street': 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    'topo': 'https://tile.opentopomap.org/{z}/{x}/{y}.png',
    'satellite': 'https://server.arcgisonline.com/ArcGIS/rest/services/'
                 'World_Imagery/MapServer/tile/{z}/{y}/{x}',
}

TILE_CACHE = {}
TILE_CACHE_DIR = os.path.join(tempfile.gettempdir(), 'gopro_tile_cache')
USER_AGENT = 'gopro-map-overlay/1.0 (https://github.com/MikeMontana1968/gopro-map-overlay)'


def _ll_to_tile(lat, lon, zoom):
    """Convert lat/lon to fractional tile coordinates at the given zoom."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = (lon + 180) / 360 * n
    y = (1 - math.log(math.tan(lat_rad) + 1/math.cos(lat_rad)) / math.pi) / 2 * n
    return x, y


def _fetch_tile(tx, ty, zoom, map_type):
    """Fetch a 256x256 map tile with memory + disk caching."""
    key = (tx, ty, zoom, map_type)
    if key in TILE_CACHE:
        return TILE_CACHE[key]

    # Check disk cache
    cache_dir = os.path.join(TILE_CACHE_DIR, map_type, str(zoom))
    cache_path = os.path.join(cache_dir, f"{tx}_{ty}.png")
    if os.path.isfile(cache_path):
        img = Image.open(cache_path).convert('RGB')
        TILE_CACHE[key] = img
        return img

    url = TILE_SERVERS[map_type].format(z=zoom, x=tx, y=ty)
    try:
        req = Request(url, headers={'User-Agent': USER_AGENT})
        resp = urlopen(req, timeout=5)
        tile_data = resp.read()
        img = Image.open(BytesIO(tile_data)).convert('RGB')
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, 'wb') as f:
            f.write(tile_data)
    except Exception:
        img = Image.new('RGB', (256, 256), (200, 200, 200))
    TILE_CACHE[key] = img
    return img


def prefetch_tiles(points, zoom_levels, map_size, map_type):
    """Pre-fetch all tiles needed for the route at all zoom levels."""
    import time
    needed = set()
    padding = 2
    tiles_needed = (map_size // 256) + padding + 1
    half = tiles_needed // 2

    for z in zoom_levels:
        for p in points:
            cx, cy = _ll_to_tile(p['lat'], p['lon'], z)
            center_tx, center_ty = int(cx), int(cy)
            for dx in range(-half, half + 1):
                for dy in range(-half, half + 1):
                    needed.add((center_tx + dx, center_ty + dy, z))

    # Remove already-cached tiles
    to_fetch = []
    for tx, ty, z in needed:
        key = (tx, ty, z, map_type)
        if key in TILE_CACHE:
            continue
        cache_path = os.path.join(TILE_CACHE_DIR, map_type, str(z), f"{tx}_{ty}.png")
        if os.path.isfile(cache_path):
            continue
        to_fetch.append((tx, ty, z))

    if not to_fetch:
        print(f"  All {len(needed)} tiles already cached", flush=True)
        return

    print(f"  {len(needed)} tiles needed, {len(to_fetch)} to download...", flush=True)
    for i, (tx, ty, z) in enumerate(to_fetch):
        _fetch_tile(tx, ty, z, map_type)
        if (i + 1) % 50 == 0:
            print(f"    {i + 1}/{len(to_fetch)} tiles", flush=True)
        time.sleep(0.05)  # rate limit: max 20 req/sec
    print(f"  Tile prefetch complete", flush=True)


CRUISE_ZOOM = 15  # ~1-2 mile context, good for 30-70 mph driving
INTRO_ZOOM_START = 7  # state-level overview for the intro animation
INTRO_DURATION = 10.0  # seconds for the zoom-in intro


def _bearing(lat1, lon1, lat2, lon2):
    """Compute forward bearing in degrees (0=north, 90=east) between two points."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.degrees(math.atan2(x, y)) % 360


def _get_font(size):
    """Try to load a system TrueType font; fall back to Pillow default."""
    for name in ['arialbd.ttf', 'arial.ttf', 'calibrib.ttf', 'segoeui.ttf',
                 'DejaVuSans-Bold.ttf', 'DejaVuSans.ttf']:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_map(center_lat, center_lon, trail, map_size, zoom, map_type,
               timestamp=None, speed_mph=None, heading=None):
    """Render a single map frame with trail, position dot, and info bar.

    If heading is provided (degrees, 0=north), the map is rotated so the
    direction of travel points up.
    """
    cx, cy = _ll_to_tile(center_lat, center_lon, zoom)
    # Fetch extra tiles to allow rotation without blank corners
    padding = 2
    tiles_needed = (map_size // 256) + padding + 1
    half = tiles_needed // 2
    canvas_size = tiles_needed * 256
    canvas = Image.new('RGB', (canvas_size, canvas_size))

    center_tx, center_ty = int(cx), int(cy)
    for dx in range(-half, half + 1):
        for dy in range(-half, half + 1):
            tile = _fetch_tile(center_tx + dx, center_ty + dy, zoom, map_type)
            canvas.paste(tile, ((dx + half) * 256, (dy + half) * 256))

    def to_px(lat, lon):
        x, y = _ll_to_tile(lat, lon, zoom)
        return int((x - center_tx + half) * 256), int((y - center_ty + half) * 256)

    draw = ImageDraw.Draw(canvas)

    # Trail line
    if len(trail) >= 2:
        px_trail = [to_px(p['lat'], p['lon']) for p in trail]
        for i in range(1, len(px_trail)):
            draw.line([px_trail[i - 1], px_trail[i]], fill=(0, 120, 255), width=3)

    # Current position dot
    cpx, cpy = to_px(center_lat, center_lon)
    r = 6
    draw.ellipse([cpx - r, cpy - r, cpx + r, cpy + r],
                 fill=(255, 0, 0), outline=(255, 255, 255), width=2)

    # Rotate so heading points up, then crop centered
    if heading is not None:
        canvas = canvas.rotate(heading, resample=Image.BICUBIC,
                               center=(cpx, cpy), fillcolor=(200, 200, 200))
        left = cpx - map_size // 2
        top = cpy - map_size // 2
    else:
        left = canvas_size // 2 - map_size // 2
        top = canvas_size // 2 - map_size // 2

    cropped = canvas.crop((left, top, left + map_size, top + map_size))

    # Info bar (semi-transparent black strip at bottom)
    if timestamp or speed_mph is not None:
        overlay = cropped.copy()
        draw2 = ImageDraw.Draw(overlay)
        bar_h = 36
        draw2.rectangle([0, map_size - bar_h, map_size, map_size], fill=(0, 0, 0))
        cropped = Image.blend(cropped, overlay, 0.65)
        draw3 = ImageDraw.Draw(cropped)
        font = _get_font(20)

        if timestamp:
            time_str = timestamp.strftime("%I:%M:%S %p")
            draw3.text((8, map_size - bar_h + 7), time_str,
                       fill=(255, 255, 255), font=font)

        if speed_mph is not None:
            spd_str = f"{speed_mph:.0f} mph"
            bbox = draw3.textbbox((0, 0), spd_str, font=font)
            tw = bbox[2] - bbox[0]
            draw3.text((map_size - tw - 8, map_size - bar_h + 7), spd_str,
                       fill=(255, 255, 255), font=font)

    return cropped


def _apply_rounded_corner(img, radius=20, fade=5):
    """Add a rounded top-left corner with alpha-blended soft edges."""
    w, h = img.size
    rgba = img.convert('RGBA')

    # Sharp rounded-rect mask (top-left rounded, other corners square)
    mask = Image.new('L', (w, h), 255)
    md = ImageDraw.Draw(mask)
    # Draw rounded rect, then fill back the three square corners
    md.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    # The rounded_rectangle rounds all four corners; restore the three we want square
    md.rectangle([w - radius, 0, w, radius], fill=255)       # top-right
    md.rectangle([0, h - radius, radius, h], fill=255)       # bottom-left
    md.rectangle([w - radius, h - radius, w, h], fill=255)   # bottom-right
    # Clear the top-left corner area, then redraw just that arc
    md.rectangle([0, 0, radius, radius], fill=0)
    md.ellipse([0, 0, radius * 2, radius * 2], fill=255)

    # Soften the mask edges
    if fade > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(fade))

    rgba.putalpha(mask)
    return rgba


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

MPS_TO_MPH = 2.23694


def main():
    parser = argparse.ArgumentParser(
        description='Generate a GPS map overlay video from a GoPro MP4.')
    parser.add_argument('input', help='Path to GoPro MP4 file')
    parser.add_argument('--map', choices=['street', 'topo', 'satellite'],
                        default='street', help='Map style (default: street)')
    parser.add_argument('--hz', type=int, default=2,
                        help='Map update rate in Hz (default: 2)')
    parser.add_argument('--zoom', type=int, default=None,
                        help='Override zoom level (auto-detected by default)')
    parser.add_argument('--size', type=int, default=None,
                        help='Map size in pixels (default: 1/4 of video height)')
    parser.add_argument('--scale', type=int, default=None,
                        help='Output height in pixels (e.g. 1080 for 1080p)')
    parser.add_argument('--no-intro', action='store_true',
                        help='Skip the zoom-in intro animation')
    parser.add_argument('--no-audio', action='store_true',
                        help='Drop all audio tracks from the output')
    parser.add_argument('--tblend', action='store_true',
                        help='Blend adjacent frames (subtle motion blur)')
    parser.add_argument('--pitch', type=float, default=None, metavar='MULT',
                        help='Exaggerate pitch rotation from accelerometer '
                             '(e.g. --pitch 3 for 3x)')
    parser.add_argument('--output', '-o', default=None,
                        help='Output file path (default: <input>_map.mp4)')
    args = parser.parse_args()

    input_mp4 = args.input
    if not os.path.isfile(input_mp4):
        print(f"Error: file not found: {input_mp4}")
        sys.exit(1)

    basename = os.path.splitext(os.path.basename(input_mp4))[0]
    output_mp4 = args.output or os.path.join(
        os.path.dirname(input_mp4), f"{basename}_map.mp4")

    # --- Step 1: Extract GPMF telemetry ---
    print("Extracting GPS telemetry...", flush=True)
    gpmf_bin = tempfile.mktemp(suffix='.bin')
    subprocess.run(
        ['ffmpeg', '-y', '-i', input_mp4, '-codec', 'copy', '-map', '0:3',
         '-f', 'rawvideo', gpmf_bin],
        capture_output=True)
    with open(gpmf_bin, 'rb') as f:
        gpmf_data = f.read()
    os.unlink(gpmf_bin)

    points = extract_all_gps(gpmf_data)
    points = [p for p in points if abs(p['lat']) > 0.1 and abs(p['lon']) > 0.1]
    print(f"  {len(points)} GPS points", flush=True)
    if not points:
        print("  No GPS data found in this file.")
        sys.exit(1)

    accl_samples = []
    if args.pitch:
        accl_samples = extract_all_accl(gpmf_data)
        print(f"  {len(accl_samples)} ACCL samples", flush=True)
        if not accl_samples:
            print("  Warning: no accelerometer data found, skipping pitch rotation")
            args.pitch = None

    # --- Step 2: Probe video metadata ---
    probe = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_streams', '-show_format', input_mp4],
        capture_output=True, text=True)
    probe_data = json.loads(probe.stdout)
    vs = [s for s in probe_data['streams'] if s['codec_type'] == 'video'][0]
    vid_w, vid_h = int(vs['width']), int(vs['height'])
    duration = float(vs['duration'])
    fps_str = vs.get('avg_frame_rate', '30/1')
    if '/' in fps_str:
        fps_n, fps_d = fps_str.split('/')
        video_fps = float(fps_n) / float(fps_d)
    else:
        video_fps = float(fps_str)

    creation_time = None
    for source in [probe_data.get('format', {}).get('tags', {}),
                   vs.get('tags', {})]:
        ct = source.get('creation_time', '')
        if ct:
            try:
                creation_time = datetime.fromisoformat(
                    ct.replace('Z', '+00:00')).replace(tzinfo=None)
            except ValueError:
                pass
            break
    if creation_time:
        print(f"  Start time: {creation_time.strftime('%I:%M:%S %p')}", flush=True)

    # Output resolution (may differ from source if --scale is used)
    if args.scale:
        out_h = args.scale
        out_w = int(vid_w * out_h / vid_h)
        out_w = out_w + (out_w % 2)  # ensure even
    else:
        out_w, out_h = vid_w, vid_h

    map_size = args.size or min(out_w, out_h) // 4
    cruise_zoom = args.zoom or CRUISE_ZOOM
    intro_frames = 0 if args.no_intro else int(INTRO_DURATION * args.hz)

    pitch_angles = []
    if args.pitch and accl_samples:
        pitch_angles = compute_pitch_angles(
            accl_samples, duration, video_fps, exaggeration=args.pitch)
        max_deg = max(abs(a) for a in pitch_angles) * 180 / math.pi
        print(f"  Roll: {len(pitch_angles)} frames, max {max_deg:.1f}° "
              f"(x{args.pitch} exaggeration)", flush=True)

    print(f"  Video: {vid_w}x{vid_h} -> {out_w}x{out_h}, {duration:.1f}s", flush=True)
    print(f"  Map: {map_size}x{map_size}px, cruise zoom {cruise_zoom}, {args.map}",
          flush=True)
    if intro_frames:
        print(f"  Intro: {INTRO_DURATION}s zoom {INTRO_ZOOM_START} -> {cruise_zoom}",
              flush=True)

    # --- Step 2b: Pre-fetch map tiles ---
    print("Pre-fetching map tiles...", flush=True)
    all_zooms = [cruise_zoom] if args.no_intro else list(range(INTRO_ZOOM_START, cruise_zoom + 1))
    # Only sample every Nth point for prefetch (no need to check all 9000+)
    sample_step = max(1, len(points) // 200)
    sample_points = points[::sample_step] + [points[-1]]
    prefetch_tiles(sample_points, all_zooms, map_size, args.map)

    # --- Step 3: Render map frames ---
    frames_dir = tempfile.mkdtemp(prefix='gopro_map_')
    total_frames = int(duration * args.hz) + 1
    print(f"Rendering {total_frames} map frames...", flush=True)

    prev_heading = 0.0

    for idx in range(total_frames):
        t = idx / args.hz
        if t > duration:
            break
        gps_idx = min(int(t / duration * (len(points) - 1)), len(points) - 1)
        current = points[gps_idx]
        trail = points[:gps_idx + 1]

        # Zoom: intro animation or cruise
        if idx < intro_frames:
            frac = idx / intro_frames
            # Ease-in-out (smootherstep — zero velocity AND acceleration at endpoints)
            frac = frac * frac * frac * (frac * (frac * 6 - 15) + 10)
            zoom = INTRO_ZOOM_START + (cruise_zoom - INTRO_ZOOM_START) * frac
            # render_map needs int zoom for tile fetching, so we step
            zoom = int(round(zoom))
        else:
            zoom = cruise_zoom

        # Heading: direction of travel (bearing from recent GPS points)
        if gps_idx > 0:
            # Average over a few points to smooth jitter
            look_back = max(0, gps_idx - 5)
            hdg = _bearing(points[look_back]['lat'], points[look_back]['lon'],
                           current['lat'], current['lon'])
            # Smooth heading transitions
            prev_heading = prev_heading + 0.3 * (((hdg - prev_heading + 180) % 360) - 180)
        heading = prev_heading

        ts = (creation_time + timedelta(seconds=t)) if creation_time else None
        speed_mph = current['speed'] * MPS_TO_MPH if current.get('speed') else None

        img = render_map(current['lat'], current['lon'], trail,
                         map_size, zoom, args.map,
                         timestamp=ts, speed_mph=speed_mph, heading=heading)

        frame = _apply_rounded_corner(img, radius=20, fade=5)
        frame.save(os.path.join(frames_dir, f"frame_{idx:06d}.png"))

        if (idx + 1) % 100 == 0:
            print(f"  {idx + 1}/{total_frames}", flush=True)

    # --- Step 4: Composite map onto source video ---
    print("Compositing onto source video...", flush=True)
    input_pattern = os.path.join(frames_dir, 'frame_%06d.png')
    margin = 10
    overlay_x = out_w - map_size - margin
    overlay_y = out_h - map_size - margin

    # Build filter chain piece by piece
    parts = []
    video_label = "[0:v]"

    if args.scale:
        parts.append(f"[0:v]scale={out_w}:{out_h}[base]")
        video_label = "[base]"

    pitch_cmdfile = None
    if pitch_angles:
        pitch_cmdfile = os.path.join(os.path.dirname(input_mp4),
                                     '_pitch_cmd.txt')
        _write_pitch_cmdfile(pitch_cmdfile, pitch_angles, video_fps)
        cmd_rel = os.path.basename(pitch_cmdfile)
        parts.append(f"{video_label}sendcmd=f={cmd_rel}[_cmd]")
        parts.append(f"[_cmd]rotate=a=0:ow=iw:oh=ih:fillcolor=black[rotated]")
        video_label = "[rotated]"

    parts.append(f"[1:v]fps={args.hz}[map]")

    overlay_out = "[comp]" if args.tblend else ""
    parts.append(
        f"{video_label}[map]overlay={overlay_x}:{overlay_y}"
        f":shortest=1{overlay_out}")

    if args.tblend:
        parts.append("[comp]tblend=all_mode=average")

    filter_str = ";".join(parts)

    audio_args = ['-an'] if args.no_audio else ['-c:a', 'aac', '-b:a', '128k']
    qsv_cmd = (
        ['ffmpeg', '-y', '-i', input_mp4,
         '-framerate', str(args.hz), '-i', input_pattern,
         '-filter_complex', filter_str,
         '-c:v', 'h264_qsv', '-global_quality', '28']
        + audio_args + [output_mp4]
    )
    sw_cmd = (
        ['ffmpeg', '-y', '-i', input_mp4,
         '-framerate', str(args.hz), '-i', input_pattern,
         '-filter_complex', filter_str,
         '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
         '-pix_fmt', 'yuv420p']
        + audio_args + [output_mp4]
    )
    encode_cwd = os.path.dirname(input_mp4) or '.'
    result = subprocess.run(qsv_cmd, capture_output=True, cwd=encode_cwd)
    if result.returncode != 0:
        print("  QSV unavailable, falling back to libx264...", flush=True)
        result = subprocess.run(sw_cmd, capture_output=True, cwd=encode_cwd)
        if result.returncode != 0:
            print(f"  Encode error: {result.stderr[-500:]}", flush=True)
    else:
        print("  Encoded with Intel QSV", flush=True)

    if pitch_cmdfile and os.path.isfile(pitch_cmdfile):
        os.unlink(pitch_cmdfile)

    shutil.rmtree(frames_dir)
    if not os.path.isfile(output_mp4):
        print("\nError: encoding failed, no output produced.", flush=True)
        sys.exit(1)
    size_mb = os.path.getsize(output_mp4) / (1024 * 1024)
    print(f"\nDone! {output_mp4}", flush=True)
    print(f"  {size_mb:.1f} MB, {duration:.0f}s at {args.hz} fps", flush=True)


if __name__ == '__main__':
    main()
