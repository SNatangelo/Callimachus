"""End-to-end signed owner choices through the desktop stderr adapter."""
import ast
import codecs
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import socket
import sqlite3
import sys
import time
from types import ModuleType, SimpleNamespace, MethodType
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
KEY="ab"*32

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module
    spec.loader.exec_module(module)
    parent,_,leaf=name.rpartition(".")
    if parent in sys.modules:setattr(sys.modules[parent],leaf,module)
    return module

class ConfirmationFlowTests(unittest.TestCase):
    def setUp(self):
        stack=ExitStack();self.addCleanup(stack.close)
        packages={}
        for name in ("core","core.fetch","core.fetch.transport","core.gui"):
            package=ModuleType(name);package.__path__=[];packages[name]=package
        stack.enter_context(mock.patch.dict(sys.modules,packages))
        self.wire=load("core.fetch.transport.network_consent",ROOT/"core/fetch/transport/network_consent.py")
        self.ui=load("core.gui.network_permissions",ROOT/"core/gui/network_permissions.py")
        self.transport=load("core.fetch.transport.public_http",ROOT/"core/fetch/transport/public_http.py")
        self.state={"json":"[]"};self.prompts=[];self.order=[];self.console=[]
        self.store=self.ui.PermissionStore(lambda:self.state["json"],self.save)
        source=ROOT/"core/gui/desktop.py"
        tree=ast.parse(source.read_text(encoding="utf-8"))
        selected=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name in {"_read_output","_get_network_permissions_store"}]
        namespace={"__package__":"core.gui","time":time,"text":{"current_activity":"{activity}"},"_cooldown_seconds":lambda _:None,"_safe_activity_text":lambda data:data.decode().strip()}
        exec(compile(ast.Module(body=selected,type_ignores=[]),str(source),"exec"),namespace)
        self.process=SimpleNamespace(stderr=b"",readAllStandardOutput=lambda:b"")
        def stderr():
            value=self.process.stderr;self.process.stderr=b"";return value
        self.process.readAllStandardError=stderr
        self.window=SimpleNamespace(_process=self.process,_analysis_stdout_decoder=codecs.getincrementaldecoder("utf-8")(),_analysis_stderr_decoder=codecs.getincrementaldecoder("utf-8")(),_network_consent_controller=None,_cooldown_until=None,_update_cooldown_status=mock.Mock(),current_activity=SimpleNamespace(setText=mock.Mock()),_append_console_output=self.console.append)
        self.namespace=namespace
        self.window.read_output=MethodType(namespace["_read_output"],self.window)
        outer=self
        class Stream:
            def write(self,text):
                data=text.encode("utf-8")
                for block in (data[:10],data[10:]):
                    outer.process.stderr=block;outer.window.read_output(stream="stderr")
            def flush(self):pass
        self.stream=Stream()
        self.addCleanup(lambda:self.wire.configure("invalid",None,None))

    def save(self,value):
        self.order.append("persist");self.state["json"]=value

    def start(self,choices,*,key=KEY,fail_audit=False,audit_callback=None):
        def ask(scope):
            self.prompts.append(scope)
            return choices.pop(0)
        def audit(event):
            self.order.append("audit")
            if fail_audit:raise OSError("dummy audit failure")
            if audit_callback is not None:audit_callback(event)
        self.window._network_consent_controller=self.ui.PermissionController(key,self.store,ask,lambda line:self.wire.handle_control_line(line+"\n"))
        self.wire.configure(key,self.stream,audit)

    def connect(self,ip="10.0.0.2",*,proxy=False,origin="http://library.example/article"):
        outer=self
        class Socket:
            def __init__(self,*args):pass
            def settimeout(self,value):pass
            def bind(self,address):raise AssertionError("OS route was overridden")
            def connect(self,address):
                outer.order.append("connect");self.address=address
            def close(self):pass
        with mock.patch.object(self.transport.socket,"getaddrinfo",return_value=[(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,"",(ip,80))]),mock.patch.object(self.transport.socket,"socket",Socket):
            conn=self.transport._http_connection("proxy.example:80" if proxy else "library.example:80",origin_url=origin,via_proxy=proxy,timeout=5)
            return conn._create_connection((conn.host,80),5)

    def test_cancel_is_denied_without_connecting_or_remembering(self):
        self.start(["deny"])
        with self.assertRaises(self.transport.PublicFetchPolicyError):self.connect()
        self.assertNotIn("connect",self.order);self.assertEqual(self.store.entries(),[])
        self.assertNotIn(self.wire.REQUEST_PREFIX,"".join(self.console))

    def test_once_requires_another_confirmation_for_the_next_request(self):
        self.start(["once","deny"])
        self.assertEqual(self.connect().address,("10.0.0.2",80))
        with self.assertRaises(self.transport.PublicFetchPolicyError):self.connect()
        self.assertEqual(len(self.prompts),2);self.assertEqual(self.order.count("connect"),1)
        self.assertEqual(self.state["json"],"[]")

    def test_session_survives_worker_restart_but_not_app_store_restart(self):
        self.start(["session"]);self.connect()
        self.start([],key="cd"*32);self.connect()
        self.assertEqual(len(self.prompts),1);self.assertEqual(self.state["json"],"[]")
        self.store=self.ui.PermissionStore(lambda:self.state["json"],self.save)
        self.start(["deny"],key="ef"*32)
        with self.assertRaises(self.transport.PublicFetchPolicyError):self.connect()
        self.assertEqual(len(self.prompts),2)

    def test_always_survives_app_store_restart_and_revocation_removes_it(self):
        self.start(["always"]);self.connect()
        self.assertEqual(self.order[:3],["persist","audit","connect"])
        self.store=self.ui.PermissionStore(lambda:self.state["json"],self.save)
        self.start([],key="cd"*32);self.connect()
        self.assertEqual(len(self.prompts),1)
        self.store.forget(self.store.entries()[0]["key"])
        self.store=self.ui.PermissionStore(lambda:self.state["json"],self.save)
        self.start(["deny"],key="ef"*32)
        with self.assertRaises(self.transport.PublicFetchPolicyError):self.connect()
        self.assertEqual(len(self.prompts),2)

    def test_changed_ip_or_proxy_origin_requires_a_new_choice(self):
        self.start(["session","deny","session","deny"])
        self.connect()
        with self.assertRaises(self.transport.PublicFetchPolicyError):self.connect("10.0.0.3")
        self.connect("10.0.0.4",proxy=True,origin="http://publisher.example/article")
        with self.assertRaises(self.transport.PublicFetchPolicyError):
            self.connect("10.0.0.4",proxy=True,origin="http://other-publisher.example/article")
        self.assertEqual(len(self.prompts),4)

    def test_audit_failure_prevents_connect_even_after_user_confirmation(self):
        self.start(["once"],fail_audit=True)
        with self.assertRaises(self.transport.PublicFetchPolicyError):self.connect()
        self.assertEqual(self.order,["audit"])

    def test_signed_confirmation_reaches_actual_driver_repository_and_sqlite(self):
        helpers=load("_network_storage_flow_tests",ROOT/"tests/test_security_network_permission_storage.py")
        storage=helpers._load_storage()
        connection=sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory=sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE run_sessions(session_id TEXT PRIMARY KEY)")
        connection.executescript(storage.NETWORK_PERMISSION_DDL+storage.NETWORK_PERMISSION_TRIGGERS)
        repository_type=helpers._load_repository_probe(storage)
        repository=repository_type();repository._conn=connection
        repository_type.active=repository
        callback,_=helpers._load_driver_callback(repository_type)
        self.start(["session"],audit_callback=callback)
        self.connect()
        records=storage.list_decisions(connection)
        self.assertEqual(self.order,["audit","connect"])
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]["event"]["choice"],"session")
        self.assertEqual(records[0]["event"]["scope"]["addresses"],["10.0.0.2"])

    def test_native_settings_adapter_retries_with_fresh_error_status(self):
        state={"json":"[]","fail":False}
        class Settings:
            Status=SimpleNamespace(NoError=0)
            def __init__(self,*args):self.error=0;self.pending=None
            def value(self,key,default):return state["json"]
            def setValue(self,key,value):self.pending=value
            def sync(self):
                if state["fail"]:self.error=1
                elif self.pending is not None:state["json"]=self.pending
            def status(self):return self.error
        self.namespace["QtCore"]=SimpleNamespace(QSettings=Settings)
        self.window._network_permissions_store=None
        get_store=MethodType(self.namespace["_get_network_permissions_store"],self.window)
        store=get_store()
        scope={"origin":"http://library.example:80","connection_host":"library.example","connection_port":80,"addresses":["10.0.0.2"],"via_proxy":False}
        self.assertTrue(store.decide(scope,"always"))
        key=self.wire.scope_key(scope);state["fail"]=True
        with self.assertRaises(OSError):store.forget(key)
        self.assertIsNone(store.remembered(scope))
        state["fail"]=False;store.forget(key)
        self.assertEqual(state["json"],"[]")
        self.window._network_permissions_store=None
        self.assertIsNone(get_store().remembered(scope))
