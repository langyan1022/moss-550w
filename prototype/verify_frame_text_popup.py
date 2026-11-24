import asyncio
import base64
from datetime import datetime
from io import BytesIO
import json
import os
from urllib.parse import urlsplit

import av
from PIL import Image, ImageChops
from playwright.async_api import async_playwright

from browser_lab import ROOT, confirm_muted_edge, open_browser
from local_media.library import MediaLibrary
from model_catalog import SPECS
from verify_configured_mcp import isolated
from verify_memory import send
from verify_video_frame_text import check_hits, make_video


FRAME_PROBE = """() => {
    const selector = '#tool-player .projection-player > video';
    const seen = new WeakSet();
    window.projectionFrames = {frames: [], playing: [], image: null};
    const probe = window.projectionFrames;
    function watch(video) {
        if (!video.matches?.(selector) || seen.has(video)) return;
        seen.add(video);
        function frame(_, metadata) {
            const {mediaTime, presentedFrames} = metadata;
            probe.frames.push({mediaTime, presentedFrames, paused: video.paused, seeking: video.seeking,
                readyState: video.readyState, afterPlaying: probe.playing.length > 0});
            if (!probe.image && mediaTime >= 8 && mediaTime < 10) {
                const canvas = document.createElement('canvas');
                canvas.width = video.videoWidth; canvas.height = video.videoHeight;
                canvas.getContext('2d').drawImage(video, 0, 0);
                probe.image = canvas.toDataURL('image/png');
            }
            if (mediaTime < 10 && video.isConnected) video.requestVideoFrameCallback(frame);
        }
        video.requestVideoFrameCallback(frame);
    }
    new MutationObserver(() => document.querySelectorAll(selector).forEach(watch))
        .observe(document, {childList:true, subtree:true});
    document.addEventListener('loadedmetadata', event => watch(event.target), true);
    document.addEventListener('playing', event => {
        const video = event.target;
        if (video.matches?.(selector)) probe.playing.push({at:video.currentTime, source:video.currentSrc});
    }, true);
}"""


def mask(image):
    return image.convert("L").point(lambda p: 255 if p < 128 else 0)


async def run():
    folder = ROOT / 'runs' / datetime.now().strftime('video-frame-popup-%Y%m%d-%H%M%S-%f')
    local = folder / 'local'
    local.mkdir(parents=True)
    disk = local / '两张标题.mp4'
    make_video(disk, [(2, 4, '北京天气 第一段'), (8, 10, '北京天气 第二段')])
    db = folder / 'index.sqlite3'
    os.environ.update(MOSS_MEDIA_DB=str(db), MOSS_PROVIDER_DIR=str(folder / 'providers'), LOCALAPPDATA=str(folder / 'profile'),
                      MOSS_WINDOWS_REMINDERS='0', MOSS_MEM0_ENABLED='0', MOSS_SPEECH_PREPARE='0')
    for _, prefixes, _, _ in SPECS.values():
        for prefix in prefixes:
            os.environ[prefix + '_BASE_URL'] = 'http://127.0.0.1:9/v1'
    library = MediaLibrary(db)
    library.configure([{'id':'disk', 'kind':'local', 'path':str(local)}])
    assert library.scan()[0]['state'] == 'ready'
    server = isolated(folder / 'app')
    report = {'passed':False}
    print(folder, flush=True)
    try:
        await server.start()
        async with async_playwright() as pw:
            edge = await open_browser(pw)
            try:
                await confirm_muted_edge(edge)
                page = await edge.new_page(viewport={'width':1200,'height':800}, reduced_motion='reduce')
                page.set_default_timeout(12000)
                await page.add_init_script("localStorage.setItem('moss.voice.enabled','off')")
                await page.goto(server.url + '/?chat=1&scene=off', wait_until='domcontentloaded')
                await page.evaluate(FRAME_PROBE)
                await send(page, server, '查《两张标题》画面里的文字“北京天气”')
                turn = (await server.state())['turns'][-1]
                _, second = check_hits(turn, '北京天气', 'disk', [2, 8])
                panel = page.get_by_test_id('media-projection')
                if not await panel.is_visible():
                    await page.get_by_test_id('open-media').last.click()
                choices = panel.get_by_test_id('track-choice')
                assert await choices.count() == 2 and '00:08.000' in await choices.nth(1).inner_text()
                await choices.nth(1).click()
                player = panel.locator('#tool-player .projection-player > video')
                await player.wait_for(state='visible')
                await page.wait_for_function('''() => projectionFrames.frames.filter(f => f.afterPlaying && !f.paused &&
                    !f.seeking && f.readyState >= 2 && f.mediaTime >= 8 && f.mediaTime < 10).length >= 2''')
                await page.screenshot(path=folder / 'popup.png')
                await page.wait_for_function("() => {const v=document.querySelector('#tool-player video'); return v.paused && v.currentTime >= 9.8;}")
                probe = await page.evaluate('projectionFrames')
                png = base64.b64decode(probe.pop('image').split(',', 1)[1])
                (folder / 'presented-frame.png').write_bytes(png)
                report.update(probe, stopped_s=await player.evaluate('v=>v.currentTime'))
                assert 9.8 <= report['stopped_s'] <= 10.3
                assert probe['playing'] and all(8 <= p['at'] < 10 and urlsplit(p['source']).path == '/media/' + second['media_id']
                                                and urlsplit(p['source']).fragment == 't=8,10' for p in probe['playing'])
                assert any(f['mediaTime'] == 8 for f in probe['frames']), probe
                moving = [f for f in probe['frames'] if f['afterPlaying'] and not f['paused'] and not f['seeking'] and 8 <= f['mediaTime'] < 10]
                assert moving[1]['mediaTime'] > moving[0]['mediaTime'] and moving[1]['presentedFrames'] > moving[0]['presentedFrames']
                with Image.open(BytesIO(png)) as image, av.open(str(disk)) as content:
                    actual = mask(image)
                    errors = {int(f.time):ImageChops.difference(actual, mask(f.to_image())).histogram()[255]
                              for f in content.decode(video=0) if f.time in (2, 8)}
                assert errors[8] < 80 and errors[2] > errors[8] + 100, errors
                turns = (await server.state())['turns']
                selected = next(t for t in reversed(turns) if t.get('media', {}).get('autoplay'))
                assert selected['media']['selected'] == second and selected['media']['playback']['clip_start_s'] == 8
                assert selected['browser_playback']['media_id'] == second['media_id']
                report.update(pixel_errors=errors, media_id=second['media_id'], browser_receipt=selected['browser_playback'],
                              cloud_requests=[t['cloud_requests']['total'] for t in turns])
                assert not any(report['cloud_requests'])
                report['passed'] = True
            finally:
                await edge.close()
    finally:
        await server.stop()
        (folder / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(folder, report['passed'], flush=True)


if __name__ == '__main__':
    asyncio.run(run())
