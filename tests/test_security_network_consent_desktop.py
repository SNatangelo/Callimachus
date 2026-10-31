"""Desktop control/audit adapters, with no app startup or native UI."""
import ast
import io
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]

def load_driver():
    source=ROOT/"core/app/run.py"
    tree=ast.parse(source.read_text(encoding="utf-8"))
    names={"_desktop_control_reader","_start_desktop_control_reader","_record_network_permission"}
    selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    scope={"_thread":SimpleNamespace(interrupt_main=mock.Mock()),"os":os,"sys":SimpleNamespace(stdin=io.StringIO(),stderr=io.StringIO()),"threading":SimpleNamespace(Thread=mock.Mock()),"_ACTIVE_RUN_DIR":"dummy-run","_ACTIVE_DRIVER_SESSION":("dummy-run","driver-session")}
    exec(compile(ast.Module(body=selected,type_ignores=[]),str(source),"exec"),scope)
    return scope

class DesktopNetworkConsentTests(unittest.TestCase):
    def setUp(self):
        self.wire=SimpleNamespace(CONTROL_ENV="CALLIMACHUS_NETWORK_CONSENT_KEY",cancel_pending=mock.Mock(),handle_control_line=mock.Mock(),configure=mock.Mock())
        transport=ModuleType("core.fetch.transport");transport.network_consent=self.wire
        self.patch=mock.patch.dict("sys.modules",{"core.fetch.transport":transport})
        self.patch.start();self.addCleanup(self.patch.stop)
        self.scope=load_driver()

    def test_stop_cancels_pending_before_interrupt(self):
        order=[]
        self.wire.cancel_pending.side_effect=lambda:order.append("cancel")
        self.scope["_desktop_control_reader"](io.StringIO("STOP\n"),interrupt=lambda:order.append("interrupt"))
        self.assertEqual(order,["cancel","interrupt"])

    def test_control_lines_forwarded_and_eof_cancels(self):
        self.scope["_desktop_control_reader"](io.StringIO("signed-control\n"))
        self.wire.handle_control_line.assert_called_once_with("signed-control\n")
        self.wire.cancel_pending.assert_called_once()

    def test_closed_control_pipe_cancels(self):
        stream=io.StringIO();stream.close()
        self.scope["_desktop_control_reader"](stream)
        self.wire.cancel_pending.assert_called_once()

    def test_desktop_key_is_removed_before_worker_environment_can_be_inherited(self):
        with mock.patch.dict(os.environ,{"CALLIMACHUS_DESKTOP_CONTROL":"1",self.wire.CONTROL_ENV:"a"*64},clear=True):
            self.scope["_start_desktop_control_reader"]()
            self.assertNotIn(self.wire.CONTROL_ENV,os.environ)
            self.assertNotIn("CALLIMACHUS_DESKTOP_CONTROL",os.environ)
        self.wire.configure.assert_called_once_with("a"*64,self.scope["sys"].stderr,self.scope["_record_network_permission"])
        self.scope["threading"].Thread.return_value.start.assert_called_once()

    def test_non_desktop_process_does_not_enable_confirmation(self):
        with mock.patch.dict(os.environ,{self.wire.CONTROL_ENV:"a"*64},clear=True):
            self.scope["_start_desktop_control_reader"]()
            self.assertNotIn(self.wire.CONTROL_ENV,os.environ)
        self.wire.configure.assert_not_called()

    def audit(self, fail=False):
        repo=SimpleNamespace(append_network_permission=mock.Mock(),close=mock.Mock())
        if fail:repo.append_network_permission.side_effect=OSError("dummy audit unavailable")
        db=ModuleType("core.infra.db");db.RunRepository=SimpleNamespace(open=mock.Mock(return_value=repo))
        with mock.patch.dict("sys.modules",{"core.infra.db":db}):
            if fail:
                with self.assertRaises(OSError):self.scope["_record_network_permission"]({"allowed":True})
            else:self.scope["_record_network_permission"]({"allowed":True})
        repo.close.assert_called_once()
        return repo

    def test_operator_decision_recorded_in_current_run_and_session(self):
        repo=self.audit()
        repo.append_network_permission.assert_called_once_with({"allowed":True},session_id="driver-session")

    def test_audit_write_failure_remains_visible_to_broker(self):
        self.audit(fail=True)

    def test_no_active_run_cannot_authorize(self):
        self.scope["_ACTIVE_RUN_DIR"]=None
        with self.assertRaises(RuntimeError):self.scope["_record_network_permission"]({"allowed":True})
