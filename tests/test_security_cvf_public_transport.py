"""CVF resolver must use the same destination policy as automatic fetch."""
import ast
import contextlib
import io
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock
import urllib.error
import urllib.request

SOURCE=Path(__file__).resolve().parents[1]/"core/resolve/providers/cvf.py"

def load_fetch():
    tree=ast.parse(SOURCE.read_text(encoding="utf-8"))
    function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="_direct_fetch_html")
    scope={"urllib": __import__("urllib.request"), "time": SimpleNamespace(monotonic=lambda:0.0)}
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(SOURCE),"exec"),scope)
    return scope["_direct_fetch_html"]

class CVFPublicTransportTests(unittest.TestCase):
    def exercise(self, opener):
        telemetry=SimpleNamespace(physical_request=lambda **kw:contextlib.nullcontext(),record_attempt=mock.Mock())
        resolve=ModuleType("core.resolve");resolve.transport_telemetry=telemetry
        headers=ModuleType("core.fetch.transport.http_headers");headers.open_request=opener
        with mock.patch.dict(sys.modules,{"core.resolve":resolve,"core.fetch.transport.http_headers":headers}), mock.patch.object(urllib.request,"urlopen",side_effect=AssertionError("unprotected direct request")):
            result=load_fetch()("https://openaccess.thecvf.com/menu", timeout=12)
        opener.assert_called_once()
        request=opener.call_args.args[0]
        self.assertEqual(request.full_url,"https://openaccess.thecvf.com/menu")
        self.assertEqual(opener.call_args.kwargs["timeout"],12)
        self.assertEqual(telemetry.record_attempt.call_count,1)
        return result

    def test_public_cvf_response_uses_protected_opener_and_telemetry(self):
        response=io.BytesIO(b"public conference index");response.status=200
        self.assertEqual(self.exercise(mock.Mock(return_value=response)),b"public conference index")

    def test_policy_denial_does_not_fall_back_to_direct_request(self):
        self.assertIsNone(self.exercise(mock.Mock(side_effect=urllib.error.URLError("public destination policy"))))
