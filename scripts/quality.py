"""Porownanie tych samych klatek po kompletnej transmisji UDP.

PSNR, SSIM i VMAF dostaja jedna pare sekwencji Y4M, z jednakowym FPS
bez duplikowania klatek. Niepelna transmisja nie daje miarodajnej pary:
metryki transportu pozostaja, jakosc jest pusta z jawnym statusem.
"""
import json
import math
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


def frames(path):
    p = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_frames',
         '-show_entries', 'stream=nb_read_frames', '-of', 'csv=p=0', str(path)],
        capture_output=True, text=True, check=True,
    )
    value = p.stdout.strip()
    return int(value) if value.isdigit() else 0


def quality_status(sender, receiver, reference_frames, output_frames):
    if (sender['pakiety_wyslane'] != receiver['pakiety_odebrane']
            or receiver['pakiety_odrzucone'] != 0
            or not receiver['_kolejnosc_ok']):
        return 'NIEWAZNA_TRANSMISJA'
    if reference_frames <= 0 or reference_frames != output_frames:
        return 'NIEZGODNE_KLATKI'
    return 'OK'


def metrics_from_logs(scratch, count):
    with (scratch / 'vmaf.json').open() as fh:
        vmaf = json.load(fh)
    mse = [float(v) for v in re.findall(r'\bmse_avg:([0-9.]+)',
                                      (scratch / 'psnr.log').read_text())]
    ssim = [float(v) for v in re.findall(r'\bAll:(-?[0-9.]+)',
                                       (scratch / 'ssim.log').read_text())]
    if not (len(mse) == len(ssim) == len(vmaf['frames']) == count):
        raise RuntimeError(f'Niezgodna liczba porownanych klatek: '
                           f'PSNR={len(mse)}, SSIM={len(ssim)}, '
                           f'VMAF={len(vmaf["frames"])}, oczekiwano={count}')
    mean_mse = sum(mse) / len(mse)
    # Y4M powstaje w yuv420p (8 bitow). Zachowaj idealne dopasowanie jako inf.
    psnr = 10 * math.log10(255 ** 2 / mean_mse) if mean_mse else 'inf'
    return {'vmaf': round(vmaf['pooled_metrics']['vmaf']['mean'], 4),
            'psnr_dB': round(psnr, 4) if isinstance(psnr, float) else psnr,
            'ssim': round(sum(ssim) / len(ssim), 6)}


def main():
    source, output, scratch_arg, fps_arg = sys.argv[1:]
    scratch = Path(scratch_arg)
    fps = Fraction(fps_arg)
    if fps <= 0:
        raise ValueError('FPS musi byc dodatni')
    sender = json.loads((scratch / 'sender.json').read_text())
    receiver = json.loads((scratch / 'receiver.json').read_text())
    ref_n, out_n = frames(source), frames(output)
    result = {'fps': str(fps), 'klatki_ref': ref_n, 'klatki_wy': out_n,
              'status': quality_status(sender, receiver, ref_n, out_n)}
    if result['status'] == 'OK':
        # Te same pozycje klatek i ta sama podstawa czasu dla wszystkich metryk.
        for src, dst in [(source, 'ref.y4m'), (output, 'out.y4m')]:
            subprocess.run([
                'ffmpeg', '-v', 'error', '-y', '-i', src, '-an',
                '-vf', f'setpts=N*{fps.denominator}/({fps.numerator}*TB),format=yuv420p',
                '-fps_mode', 'passthrough', str(scratch / dst),
            ], check=True)
        subprocess.run([
            'vmaf', '-r', str(scratch / 'ref.y4m'), '-d', str(scratch / 'out.y4m'),
            '--model', 'version=vmaf_v0.6.1', '-o', str(scratch / 'vmaf.json'),
            '--json', '--quiet',
        ], check=True)
        for metric in ['psnr', 'ssim']:
            subprocess.run([
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-i', str(scratch / 'out.y4m'), '-i', str(scratch / 'ref.y4m'),
                '-lavfi', f'{metric}=stats_file={scratch / (metric + ".log")}:shortest=1:repeatlast=0',
                '-fps_mode', 'passthrough', '-f', 'null', '-',
            ], check=True)
        result.update(metrics_from_logs(scratch, ref_n))
    (scratch / 'quality.json').write_text(json.dumps(result), encoding='utf-8')
    print('Jakosc:', result['status'], 'klatki:', ref_n, '/', out_n)


if __name__ == '__main__':
    main()
