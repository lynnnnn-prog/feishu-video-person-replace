"""Offline regressions: no provider API calls and no production data."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'worker'))
TEMP = tempfile.TemporaryDirectory()
os.environ['WORKER_DATA_DIR'] = TEMP.name
os.environ['WORKER_API_KEY'] = 'test-worker-secret'
import pipeline
import worker
from fastapi.testclient import TestClient


class WorkerTests(unittest.TestCase):
    def test_auth_rejects_missing_and_wrong_key(self):
        client = TestClient(worker.app)
        self.assertEqual(client.get('/jobs/job_test').status_code, 401)
        self.assertEqual(client.get('/jobs/job_test', headers={'X-Worker-Key': 'wrong'}).status_code, 401)
        self.assertEqual(client.get('/jobs/job_test', headers={'X-Worker-Key': 'test-worker-secret'}).status_code, 404)

    def test_request_token_not_saved(self):
        async def no_work(*args):
            return None
        request = worker.JobRequest(source_url='https://example.com/video.mp4',
            reference_image_urls=['https://example.com/ref.png'], feishu_access_token='test-sensitive-token')
        self.assertFalse(request.generate_audio)
        with patch.object(worker, 'execute_job', no_work):
            state = asyncio.run(worker.create_job(request))
        data = (worker.JOBS_DIR / state['job_id'] / 'request.json').read_text()
        self.assertNotIn('test-sensitive-token', data)
        self.assertNotIn('feishu_access_token', json.loads(data))

    def test_local_limit_rejects_aws_ten_seconds(self):
        with self.assertRaises(ValueError):
            worker.JobRequest(source_url='https://example.com/v.mp4',
                reference_image_urls=['https://example.com/r.png'], segment_seconds=10)

    def test_audio_flag_reaches_pipeline(self):
        job_id = 'job_test_audio'
        (worker.JOBS_DIR / job_id).mkdir(exist_ok=True)
        request = worker.JobRequest(source_url='https://example.com/v.mp4',
            reference_image_urls=['https://example.com/r.png'], generate_audio=False)
        called = []
        async def pipeline_stub(*args, **kwargs):
            called.append(kwargs['generate_audio'])
            return Path(TEMP.name) / 'result.mp4'
        with patch.object(worker, 'download', AsyncMock()), patch.object(worker, 'run_pipeline', pipeline_stub):
            asyncio.run(worker.execute_job(job_id, request))
        self.assertEqual(called, [False])
        self.assertEqual(worker.read_state(job_id)['status'], 'completed')

    def test_original_audio_and_silent_source(self):
        import array
        import math
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            source, edited = td/'source.mp4', td/'edited.mp4'
            # Source is 440 Hz; model output is 880 Hz. The result must retain 440 Hz.
            for path, frequency in [(source, 440), (edited, 880)]:
                pipeline.run_ffmpeg(['-f','lavfi','-i','color=c=blue:s=160x90:r=30:d=3',
                    '-f','lavfi','-i',f'sine=frequency={frequency}:duration=3',
                    '-c:v','libx264','-c:a','aac','-shortest','-y',str(path)], 'fixture')
            pipeline.prepare_visual_segment(edited,160,90,3)
            self.assertFalse(pipeline.has_audio(edited))
            concat=td/'concat.txt'; concat.write_text(f"file '{edited}'\n")
            result=td/'result.mp4'
            pipeline.merge_with_original_audio(concat,source,result)
            self.assertTrue(pipeline.has_audio(result))
            self.assertLess(abs(pipeline.duration(result)-3),0.1)
            samples=array.array('f',subprocess.check_output([pipeline.ffmpeg(),'-v','error','-i',str(result),
                '-vn','-f','f32le','-ac','1','-ar','8000','-']))
            def strength(hz):
                return abs(sum(value * complex(math.cos(2*math.pi*hz*i/8000), math.sin(2*math.pi*hz*i/8000))
                    for i,value in enumerate(samples)))
            self.assertGreater(strength(440), strength(880)*20)
            pipeline.merge_with_original_audio(concat,edited,td/'silent.mp4')
            self.assertFalse(pipeline.has_audio(td/'silent.mp4'))

    def test_duration_mismatch_is_not_silently_stretched(self):
        with patch.object(pipeline, 'duration', return_value=5):
            with self.assertRaises(RuntimeError):
                pipeline.prepare_visual_segment(Path('unused.mp4'),160,90,3)


if __name__ == '__main__':
    unittest.main()
