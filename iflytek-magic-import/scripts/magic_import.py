#!/usr/bin/env python3
"""Create the Magic recorder's tested dual-channel SBC recording bundle."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def run(args):
    return subprocess.run([str(x) for x in args], check=True, capture_output=True).stdout


def probe(path, binary):
    return json.loads(run([binary, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', path]))


TABLE = []
for v in range(256):
    c = v
    for _ in range(8):
        c = (((c << 1) ^ 0x1d) if c & 128 else c << 1) & 255
    TABLE.append(c)


def crc(data):
    c = 15
    for v in data:
        c = TABLE[c ^ v]
    return c


def require(condition, message):
    if not condition:
        raise ValueError(message)


def merge(left, right, output):
    frames = 0
    with left.open('rb') as l, right.open('rb') as r, output.open('wb') as out:
        while True:
            a, b = l.read(66), r.read(66)
            if not a and not b:
                break
            require(len(a) == len(b) == 66, 'Truncated or mismatched mono SBC frames')
            for frame in (a, b):
                require(frame[:3] == bytes.fromhex('9c311d'), 'Unexpected mono SBC parameters')
                require(crc(frame[1:3] + frame[4:8]) == frame[3], 'Mono SBC CRC mismatch')
            scales = a[4:8] + b[4:8]
            abits = f'{int.from_bytes(a[8:], "big"):0464b}'
            bbits = f'{int.from_bytes(b[8:], "big"):0464b}'
            payload = ''.join(abits[j:j+29] + bbits[j:j+29] for j in range(0, 464, 29))
            header = bytes.fromhex('351d')
            frame = b'\x9c' + header + bytes([crc(header + scales)]) + scales + int(payload, 2).to_bytes(116, 'big')
            require(len(frame) == 128, 'Invalid dual SBC frame length')
            out.write(frame)
            frames += 1
    require(frames > 0, 'Empty audio')
    return frames


def md5(path):
    h = hashlib.md5()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def convert(args):
    stamp = dt.datetime.strptime(args.recorded_at, '%Y-%m-%d %H:%M:%S')
    name = stamp.strftime('%Y-%m-%d %H_%M_%S')
    root = Path(args.output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / ('iFLY_' + name)
    require(not dest.exists(), f'Destination exists: {dest}')
    info = probe(args.input, args.ffprobe)
    audio = [s for s in info['streams'] if s['codec_type'] == 'audio']
    require(0 <= args.audio_stream < len(audio), 'Requested audio stream not available')
    # Build and validate before creating the final recording directory.
    with tempfile.TemporaryDirectory(prefix='.magic-build-', dir=root) as temp:
        temp = Path(temp)
        bundle = temp / ('iFLY_' + name)
        bundle.mkdir()
        mp3, sbc = bundle / (name + '.mp3'), bundle / (name + '.sbc')
        left, right, pcm = temp / 'left.sbc', temp / 'right.sbc', temp / 'stereo.wav'
        common = [args.ffmpeg, '-v', 'error', '-nostdin', '-i', args.input, '-map', f'0:a:{args.audio_stream}']
        if args.seconds is not None:
            require(args.seconds > 0, 'seconds must be positive')
            common += ['-t', str(args.seconds)]
        # Normalize once to preserve timing and handle mono/multichannel sources consistently.
        run(common + ['-ar', '16000', '-ac', '2', '-c:a', 'pcm_s16le', pcm])
        normalized = [args.ffmpeg, '-v', 'error', '-nostdin', '-i', pcm]
        run(normalized + ['-c:a', 'libmp3lame', '-b:a', '64k', '-map_metadata', '-1',
                          '-id3v2_version', '0', '-write_id3v1', '0', '-write_xing', '0', mp3])
        for channel, path in [(0, left), (1, right)]:
            run(normalized + ['-af', f'pan=mono|c0=c{channel}', '-c:a', 'sbc', '-b:a', '66k', path])
        frames = merge(left, right, sbc)
        duration = frames * 128 / 16000
        for path in (mp3, sbc):
            run([args.ffmpeg, '-v', 'error', '-xerror', '-nostdin', '-i', path, '-f', 'null', '-'])
        mp = probe(mp3, args.ffprobe)
        stream = mp['streams'][0]
        require(stream['codec_name'] == 'mp3' and int(stream['sample_rate']) == 16000 and stream['channels'] == 2,
                'Unexpected MP3 parameters')
        require(abs(float(mp['format']['duration']) - duration) < .2, 'Output duration mismatch')
        pcm_duration = float(probe(pcm, args.ffprobe)['format']['duration'])
        require(abs(pcm_duration - duration) < .02, 'SBC/source duration mismatch')
        with mp3.open('rb') as f:
            header = f.read(4)
        require(header[:2] == bytes.fromhex('fff3'), 'Expected raw MPEG-2 MP3 header')
        (bundle / (name + '.mark')).write_bytes(b'')
        (bundle / (name + '.mode')).write_bytes(b'{"0":0}')
        (bundle / (name + '.sbc.md5')).write_text(md5(sbc), encoding='ascii')
        hashes = {p.name: md5(p) for p in bundle.iterdir()}
        # Never replace a preexisting recording, including an empty folder.
        dest.mkdir(exist_ok=False)
        try:
            for source in bundle.iterdir():
                target = dest / source.name
                shutil.copyfile(source, target)
                require(md5(target) == hashes[source.name], 'Copied file checksum mismatch')
            for extra in dest.glob('._*'):
                extra.unlink()
        except BaseException:
            # Only this invocation's newly created destination is removed.
            shutil.rmtree(dest)
            raise
        print(json.dumps({'folder': str(dest), 'recorded_at': args.recorded_at,
                          'duration_seconds': duration, 'files_md5': hashes}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['inspect', 'convert']:
        sub = commands.add_parser(name)
        sub.add_argument('--input', required=True)
        sub.add_argument('--ffprobe', default=shutil.which('ffprobe') or 'ffprobe')
        if name == 'convert':
            sub.add_argument('--ffmpeg', default=shutil.which('ffmpeg') or 'ffmpeg')
            sub.add_argument('--recorded-at', required=True, help='Confirmed recording local time YYYY-MM-DD HH:MM:SS')
            sub.add_argument('--output-root', required=True)
            sub.add_argument('--seconds', type=float)
            sub.add_argument('--audio-stream', type=int, default=0)
    args = parser.parse_args()
    try:
        if args.command == 'inspect':
            print(json.dumps(probe(args.input, args.ffprobe), ensure_ascii=False, indent=2))
        else:
            convert(args)
    except subprocess.CalledProcessError as exc:
        parser.exit(1, exc.stderr.decode(errors='replace') + '\n')
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
