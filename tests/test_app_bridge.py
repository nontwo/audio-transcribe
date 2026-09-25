"""Native protocol contract: no raw transcript is sent to the UI."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audio_transcribe import cli, config, storage


class AppBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = {'roots':{'data':str(self.root)},'storage_approved':True}
        self.request = self.root/'request.json'

    def test_explicit_order_duplicates_and_unicode_round_trip(self):
        paths = ['/fixtures/part10 中文.m4a','/fixtures/part2.wav','/fixtures/part2.wav']
        storage.write_json(self.request,{'files':paths,'retry_failed':True})
        def build(settings, received, resolved, *, events, retry_failed):
            self.assertEqual(received, paths);self.assertTrue(retry_failed)
            events({'type':'file','index':0,'total':3,'state':'completed'})
            return {'state':'partial','report':'/fixture/transcript-report.md','selected':3,'completed':2,'failed':1}
        stream=io.StringIO()
        with patch.object(cli,'build_report',side_effect=build), contextlib.redirect_stdout(stream):
            code=cli.app_report_command(self.settings,str(self.request))
        self.assertEqual(code,1)
        events=[json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([e['type'] for e in events],['file','result'])
        self.assertFalse(any('text' in e for e in events))

    def test_error_and_cancel_are_json_without_private_exception_payload(self):
        storage.write_json(self.request,{'files':['/fixture/a.wav']})
        for error, expected, code in [(ValueError('private payload must not be emitted'),'error',2),(KeyboardInterrupt(),'cancelled',130)]:
            stream=io.StringIO()
            with patch.object(cli,'build_report',side_effect=error),contextlib.redirect_stdout(stream):
                self.assertEqual(cli.app_report_command(self.settings,str(self.request)),code)
            self.assertEqual(json.loads(stream.getvalue())['type'],expected)
            self.assertNotIn('private payload',stream.getvalue())

    def test_invalid_request_never_imports(self):
        for paths in ([],['relative.wav'],[None],'/fixture/a.wav'):
            storage.write_json(self.request,{'files':paths},overwrite=self.request.exists())
            with patch.object(cli,'build_report') as build,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.app_report_command(self.settings,str(self.request)),2)
            build.assert_not_called()

    def test_confirmed_groups_are_forwarded_without_changing_file_order(self):
        paths = ['/fixtures/audio_240102_090000.wav', '/fixtures/audio_240103_110000.wav']
        groups = [{'indices':[0], 'title':'Class A', 'date':'2024-01-02', 'confirmed':True},
                  {'indices':[1], 'title':'Class B', 'date':None, 'confirmed':True}]
        storage.write_json(self.request, {'files': paths, 'groups': groups})
        result = {'state':'completed', 'report':'/fixtures/master-report.md', 'selected':2, 'completed':2, 'failed':0}
        with patch.object(cli, 'build_report', return_value=result) as build, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.app_report_command(self.settings, str(self.request)), 0)
        self.assertEqual(build.call_args.args[1], paths)
        self.assertEqual(build.call_args.kwargs['groups'], groups)

    def test_library_bridge_emits_only_safe_error_for_invalid_request(self):
        storage.write_json(self.request, {'action':'read', 'report_id':'../private'})
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(cli.app_library_command(self.settings, str(self.request)), 2)
        self.assertEqual(json.loads(stream.getvalue())['type'], 'error')
        self.assertNotIn('../private', stream.getvalue())
