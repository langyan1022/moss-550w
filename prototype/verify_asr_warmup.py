import argparse
import asyncio
import base64
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter
import wave

import aiohttp
from playwright.async_api import async_playwright
import psutil

from browser_lab import BrowserServer, ROOT, confirm_muted_edge
from voice_input import UNUSED_WORKER_SECONDS


def workers(server):
    children=psutil.Process(server.pid).children(recursive=True)
    matches=[p for p in children if any("asr_engine.py" in s for s in p.cmdline())]
    parents={p.ppid() for p in matches}
    return [{"pid":p.pid,"rss_mib":round(p.memory_info().rss/2**20,1),
             "peak_rss_mib":round(p.memory_info().peak_wset/2**20,1),
             "worker_family_rss_mib":round(sum(c.memory_info().rss for c in matches)/2**20,1)}
            for p in matches if p.pid not in parents]


async def post(http,server,path,audio=None):
    started=perf_counter()
    async with http.post(server.url+path, data=audio,headers={
        "Content-Type":"audio/wav" if audio is not None else "application/json"}) as response:
        response.raise_for_status()
        result=await response.json() if audio is not None else None
    return {"ms":round((perf_counter()-started)*1000,1),"text":result and result["text"]}


async def real_page(server,microphone):
    async with async_playwright() as pw:
        exe=Path(os.environ["ProgramFiles(x86)"])/"Microsoft/Edge/Application/msedge.exe"
        browser=await pw.chromium.launch(executable_path=str(exe),headless=True,args=[
            "--disable-gpu","--mute-audio","--disable-background-timer-throttling","--disable-renderer-backgrounding",
            "--use-fake-device-for-media-stream","--use-file-for-fake-audio-capture="+str(microphone.resolve())])
        try:
            await confirm_muted_edge(browser)
            context=await browser.new_context(reduced_motion="reduce")
            await context.grant_permissions([], origin=server.url)
            await context.add_init_script("""
window.voiceQA={permission:[],requests:[],tracks:[]};
const getMedia=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia=async (...args)=>{
  try {
    const stream=await getMedia(...args);
    voiceQA.permission.push({ok:true,at:performance.now()});
    voiceQA.tracks.push(...stream.getAudioTracks()); return stream;
  } catch(error) {voiceQA.permission.push({ok:false,at:performance.now()});throw error;}
};
const realFetch=window.fetch;
window.fetch=async function(url,options){
  if(!['/api/prepare-voice','/api/transcribe','/api/message'].includes(url)) return realFetch.apply(this,arguments);
  const event={url,start:performance.now()}; voiceQA.requests.push(event);
  if(url==='/api/transcribe') voiceQA.clip=options.body;
  const response=await realFetch.apply(this,arguments);
  event.status=response.status;event.end=performance.now();
  if(url==='/api/transcribe') event.text=(await response.clone().json()).text;
  return response;
};
""")
            page=await context.new_page()
            external=[]
            page.on("request",lambda r:external.append(r.url) if r.url.startswith(("http:","https:")) and not r.url.startswith(server.url+"/") else None)
            await page.goto(server.url+"/?chat=1&scene=off",wait_until="domcontentloaded")
            await page.locator(".connection.online").wait_for()
            await page.get_by_test_id("composer").fill("保留草稿")
            assert not workers(server) and not await page.evaluate("voiceQA.requests.length")
            assert await page.evaluate("(async()=>(await navigator.permissions.query({name:'microphone'})).state)()")=="denied"
            await page.locator("#mic").click()
            await page.wait_for_function("voiceQA.permission.length===1 && !voiceQA.permission[0].ok")
            assert not workers(server) and not await page.evaluate("voiceQA.requests.length")
            await context.grant_permissions(["microphone"],origin=server.url)
            await page.locator("#mic").click()
            await page.locator("#mic[aria-pressed='true']").wait_for()
            await page.locator("#interrupt").click()
            await page.wait_for_function("voiceQA.tracks.length===1 && voiceQA.tracks[0].readyState==='ended'")
            await page.wait_for_function("voiceQA.requests[0]?.status===204",timeout=100000)
            cancel=await page.evaluate("""()=>({requests:voiceQA.requests,permissions:voiceQA.permission,track:voiceQA.tracks[0].readyState,
composer:document.querySelector('#message').value})""")
            assert len(cancel["requests"])==1 and cancel["requests"][0]["url"]=="/api/prepare-voice"
            assert [p["ok"] for p in cancel["permissions"]]==[False,True] and cancel["composer"]=="保留草稿"
            cancel["resident"]=workers(server)
            assert len(cancel["resident"])==1
            wait_started=perf_counter()
            while workers(server) and perf_counter()-wait_started<UNUSED_WORKER_SECONDS+10:
                await asyncio.sleep(.75)
            cancel["measured_seconds_after_ready"]=round(perf_counter()-wait_started,2)
            cancel["final_workers"]=workers(server)
            assert not cancel["final_workers"] and not (await server.state())["turns"]
            await page.evaluate("voiceQA.requests=[];voiceQA.tracks=[];voiceQA.permission.splice(1)")
            await page.locator("#mic").click()
            await page.wait_for_function("document.querySelector('#message').value.includes('用这段生成回复')",timeout=95000)
            payload=base64.b64decode(await page.evaluate("""async()=> {
                const reader=new FileReader();
                const data=new Promise((resolve,reject)=>{reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;});
                reader.readAsDataURL(voiceQA.clip);return await data;
            }"""))
            await page.locator(".preferences summary").click()
            await page.locator("#wake-listen").check()
            await page.wait_for_function("voiceQA.requests.filter(e=>e.url==='/api/prepare-voice' && e.status===204).length===2")
            await page.locator("#wake-listen").uncheck()
            await page.wait_for_function("voiceQA.tracks.every(t=>t.readyState==='ended')")
            records=await page.evaluate("""()=>({permission:voiceQA.permission,requests:voiceQA.requests,
tracks:voiceQA.tracks.map(t=>t.readyState),composer:document.querySelector('#message').value})""")
            prepare,recognition,wake=records["requests"]
            allowed=records["permission"]
            assert [p["ok"] for p in allowed]==[False,True,True],allowed
            assert [q["url"] for q in (prepare,recognition,wake)]==["/api/prepare-voice","/api/transcribe","/api/prepare-voice"]
            assert allowed[1]["at"]<=prepare["start"]<recognition["start"] and allowed[2]["at"]<=wake["start"]
            assert wake["status"]==prepare["status"]==204 and recognition["status"]==200
            assert records["composer"]=="保留草稿\n"+recognition["text"] and not external
            records["external"]=external
            records["cancel_before_upload"]=cancel
            records["post_to_text_ms"]=round(recognition["end"]-recognition["start"],1)
            records["prepare_to_post_ms"]=round(recognition["start"]-prepare["start"],1)
            records["prepare_to_ready_ms"]=round(prepare["end"]-prepare["start"],1)
            return records,payload
        finally:
            await browser.close()


async def benchmark(folder,port,clip,overlap):
    server=BrowserServer(folder,port,model_url="http://127.0.0.1:9/v1")
    await server.start()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=115)) as http:
            assert not workers(server)
            cpu=await asyncio.to_thread(psutil.cpu_percent,.5)
            free=round(psutil.virtual_memory().available/2**30,2)
            if overlap:
                tasks=[asyncio.create_task(post(http,server,"/api/prepare-voice")) for _ in range(2)]
                await asyncio.sleep(overlap)
            first=await post(http,server,"/api/transcribe",clip)
            prepares=await asyncio.gather(*tasks) if overlap else []
            process=workers(server)
            assert len(process)==1,process
            repeats=[await post(http,server,"/api/transcribe",clip) for _ in range(3)]
            assert all(row["text"]==first["text"] for row in repeats)
            assert workers(server)[0]["pid"]==process[0]["pid"] and not (await server.state())["turns"]
        return {"first":first,"prepares":prepares,"repeats":repeats,"worker":process[0],"overlap_seconds":overlap,
                "cpu_percent_before":cpu,"available_gib_before":free}
    finally:
        await server.stop()


async def run(folder,mic,port):
    folder.mkdir(exist_ok=False,parents=True)
    report={"passed":False,"physical_microphone":False,"fixture":str(mic),"provider":"cpu"}
    os.environ.update(MOSS_MEM0_ENABLED="0",MOSS_SPEECH_PREPARE="0")
    server=BrowserServer(folder/"browser",port,model_url="http://127.0.0.1:9/v1")
    try:
        await server.start()
        try:
            page,clip=await real_page(server,mic)
            report["page"]=page
            assert len(workers(server))==1 and not (await server.state())["turns"]
            report["page"]["worker"]=workers(server)[0]
            until=perf_counter()+UNUSED_WORKER_SECONDS+1
            report["clip_sha256"]=hashlib.sha256(clip).hexdigest()
            with wave.open(io.BytesIO(clip),"rb") as wav:
                seconds=wav.getnframes()/wav.getframerate()
            report["duration_seconds"]=seconds
            for mode in ("control_before","overlap","control_after"):
                print(mode,flush=True)
                report[mode]=await benchmark(folder/mode,port+1,clip,seconds if mode=="overlap" else 0)
            await asyncio.sleep(max(0,until-perf_counter()))
            report["used_after_expiry"]=workers(server)
            assert report["used_after_expiry"][0]["pid"]==report["page"]["worker"]["pid"]
        finally:
            await server.stop()
        controls=[report[k]["first"]["ms"] for k in ("control_before","control_after")]
        gain=median(controls)-report["overlap"]["first"]["ms"]
        report["post_to_text_gain_ms"]=round(gain,1)
        assert all(report[m]["first"]["text"]==page["requests"][1]["text"] for m in ("control_before","overlap","control_after"))
        report["passed"]=True
    finally:
        (folder/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(folder,report["passed"],flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--mic",type=Path,default=ROOT/"runs/context-voice-e2e-20261121-095921-986182/file-microphone.wav")
    parser.add_argument("--out",type=Path,default=ROOT/"runs"/datetime.now().strftime("asr-warmup-%Y%m%d-%H%M%S-%f"))
    parser.add_argument("--port",type=int,default=18983)
    options=parser.parse_args()
    asyncio.run(run(options.out,options.mic,options.port))
