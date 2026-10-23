"""Known-answer audio evidence report, separate from the matching results."""
import json
import sys
import threading
from pathlib import Path
from fpv_matcher.audio.quality import benchmark
from fpv_matcher.audio.fingerprint import ALGORITHM_VERSION, extract_audio, validate_audio
from fpv_matcher.cache.fingerprints import get_or_extract
from fpv_matcher.storage import atomic_json


def run(request_path):
    request=json.loads(Path(request_path).read_text())
    directory=Path(request["directory"])
    def load(record,boundary):
        path=Path(record["path"])
        stat=path.stat()
        if [stat.st_size,stat.st_mtime_ns]!=record["signature"]:
            raise ValueError("Reference source changed")
        return get_or_extract(str(path),request["cache"],"audio",ALGORITHM_VERSION,{"boundary_seconds":boundary},lambda:extract_audio(str(path),boundary),record.get("identity"),validate=validate_audio)
    def progress(percent,message):
        atomic_json(directory/"progress.json",{"percent":percent,"message":message})
    result=benchmark(request["document"],load,request["boundary"],progress,threading.Event())
    for record in request["document"]["videos"]:
        path=Path(record["path"]);stat=path.stat()
        if [stat.st_size,stat.st_mtime_ns]!=record["signature"]:
            raise ValueError("Reference source changed during validation")
    atomic_json(directory/"validation.json",result)
    progress(100,"Known-answer audio report ready")

if __name__=="__main__":
    run(sys.argv[1])
