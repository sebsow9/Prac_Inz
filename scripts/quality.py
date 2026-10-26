"""RAW: naive frame order. RTP: timestamp timeline with explicit frame holds.

Packet loss is reported independently; it never disables quality by itself.
Undecodable video produces a diagnostic record, never an invented score.
"""
import json
import math
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


def frames(path):
    p = subprocess.run([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_frames',
        '-show_entries', 'stream=nb_read_frames', '-of', 'json', str(path),
    ], capture_output=True, text=True, check=True)
    return int(json.loads(p.stdout)['streams'][0]['nb_read_frames'])


def transmission_status(sender, receiver):
    complete = (sender['pakiety_wyslane'] == receiver['pakiety_odebrane']
                and receiver['pakiety_odrzucone'] == 0
                and receiver['_kolejnosc_ok'])
    return 'KOMPLETNA' if complete else 'STRATY_LUB_ZMIANA_KOLEJNOSCI'


def rtp_offset(sender, receiver):
    first = sender.get('_rtp_first_timestamp')
    received = receiver.get('_rtp_first_timestamp')
    if first is None or received is None:
        raise ValueError('Brak poczatkowych timestampow RTP; wymagany nowy pomiar')
    ticks = ((received - first + (1 << 31)) % (1 << 32)) - (1 << 31)
    if ticks < 0:
        raise ValueError('Pierwszy odebrany timestamp poprzedza poczatek nadajnika')
    return ticks / 90000


def metrics_from_logs(scratch, count):
    vmaf = json.loads((scratch / 'vmaf.json').read_text())
    mse = [float(v) for v in re.findall(r'\bmse_avg:([0-9.]+)',
                                      (scratch / 'psnr.log').read_text())]
    ssim = [float(v) for v in re.findall(r'\bAll:(-?[0-9.]+)',
                                       (scratch / 'ssim.log').read_text())]
    if not (len(mse) == len(ssim) == len(vmaf['frames']) == count):
        raise RuntimeError(f'Niezgodne liczby klatek metryk: '
                           f'{len(mse)}/{len(ssim)}/{len(vmaf["frames"])}, oczekiwano {count}')
    mean_mse = sum(mse) / count
    return {
        'vmaf': round(vmaf['pooled_metrics']['vmaf']['mean'], 4),
        'psnr_dB': round(10 * math.log10(255 ** 2 / mean_mse), 4) if mean_mse else 'inf',
        'ssim': round(sum(ssim) / count, 6),
    }


def run(command, log):
    with log.open('ab') as errors:
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=errors, check=True)


def evaluate(source, output, scratch, fps, sender, receiver):
    transport = sender.get('transport', 'raw')
    method = ('RAW_NAIWNE_KOLEJNE_KLATKI_WSPOLNY_ZAKRES' if transport == 'raw'
              else 'RTP_PTS_30FPS_POWTORZENIA' if fps == 30
              else 'RTP_PTS_CFR_POWTORZENIA')
    result = {'fps': str(fps), 'klatki_ref': 0, 'klatki_wy': 0,
              'klatki_porownane': 0, 'status': 'BRAK_METRYK',
              'metoda': method, 'transmisja_status': transmission_status(sender, receiver)}
    log = scratch / 'quality_errors.log'
    try:
        ref_n = result['klatki_ref'] = frames(source)
        out_n = result['klatki_wy'] = frames(output)
        if min(ref_n, out_n) <= 0:
            raise ValueError('Brak zdekodowanych klatek')
        count = min(ref_n, out_n) if transport == 'raw' else ref_n
        step = f'N*{fps.denominator}/({fps.numerator}*TB)'
        ref_filter = f'setpts={step},format=yuv420p'
        if transport == 'raw':
            # Deliberately naive: lost frames shift subsequent comparisons;
            # compare only the common prefix, without pretending to align time.
            out_filter = ref_filter
            result['rtp_offset_s'] = None
        elif transport == 'rtp':
            offset = result['rtp_offset_s'] = rtp_offset(sender, receiver)
            duration = ref_n / float(fps)
            if offset >= duration:
                raise ValueError('RTP poza czasem trwania referencji')
            # No PTS-STARTPTS here: that would erase missing initial frames.
            # The first decoded frame fills a missing prefix, the last fills
            # the tail; internal gaps use FFmpeg's fps sample/hold policy.
            out_filter = (f'setpts=PTS+{offset:.12f}/TB,'
                          f'fps=fps={fps}:start_time=0:round=near,'
                          f'tpad=stop_mode=clone:stop_duration={duration:.12f},'
                          f'trim=end_frame={count},format=yuv420p')
        else:
            raise ValueError(f'Nieznany transport: {transport}')
        for src, dst, filt in [(source, 'ref.y4m', ref_filter),
                               (output, 'out.y4m', out_filter)]:
            run(['ffmpeg', '-nostdin', '-v', 'warning', '-y', '-copyts',
                 '-i', str(src), '-map', '0:v:0', '-an', '-vf', filt,
                 '-frames:v', str(count), '-fps_mode', 'passthrough',
                 str(scratch / dst)], log)
        # Also catches truncated Y4M/decode failures before invoking VMAF.
        if frames(scratch / 'ref.y4m') != count or frames(scratch / 'out.y4m') != count:
            raise RuntimeError('Nie udalo sie przygotowac pelnej pary sekwencji')
        run(['vmaf', '-r', str(scratch / 'ref.y4m'), '-d', str(scratch / 'out.y4m'),
             '--model', 'version=vmaf_v0.6.1', '-o', str(scratch / 'vmaf.json'),
             '--json', '--quiet'], log)
        for metric in ['psnr', 'ssim']:
            run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
                 '-i', str(scratch / 'out.y4m'), '-i', str(scratch / 'ref.y4m'),
                 '-lavfi', f'{metric}=stats_file={scratch / (metric + ".log")}:shortest=1:repeatlast=0',
                 '-fps_mode', 'passthrough', '-f', 'null', '-'], log)
        result.update(metrics_from_logs(scratch, count))
        result['klatki_porownane'] = count
        result['status'] = 'OK_RAW_NAIWNE' if transport == 'raw' else 'OK_RTP_OS_CZASU'
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError, RuntimeError) as error:
        result['blad'] = str(error)
        if log.exists():
            result['blad'] += '\n' + log.read_text(errors='replace')[-2000:]
    return result


def main():
    source, output, scratch_arg, fps_arg = sys.argv[1:]
    scratch = Path(scratch_arg)
    fps = Fraction(fps_arg)
    if fps <= 0:
        raise ValueError('FPS musi byc dodatni')
    sender = json.loads((scratch / 'sender.json').read_text())
    receiver = json.loads((scratch / 'receiver.json').read_text())
    result = evaluate(source, output, scratch, fps, sender, receiver)
    (scratch / 'quality.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    print('Jakosc:', result['status'], 'klatki:', result['klatki_ref'], '/',
          result['klatki_wy'], 'porownane:', result['klatki_porownane'])
    if 'blad' in result:
        print('Blad jakosci:', result['blad'], file=sys.stderr)


if __name__ == '__main__':
    main()
