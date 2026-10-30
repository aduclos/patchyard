"""Device drivers against small fake vendor APIs (no real hardware)."""
import base64
import hashlib
import io
import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs

from patchyard import devices
from patchyard.config import Device
from patchyard.devices import asuswrt, omada_eap, opnsense


class Fake(BaseHTTPRequestHandler):
    routes: ClassVar[dict] = {}
    calls: ClassVar[list] = []

    def log_message(self, *a):
        pass

    def _go(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        self.calls.append((method, self.path, body, dict(self.headers)))
        fn = self.routes.get((method, self.path.split("?")[0]))
        code, payload, extra = fn(self, body) if fn else (404, {}, {})
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        for k, v in extra.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")


class DeviceTestBase(unittest.TestCase):
    def serve(self, routes):
        Fake.routes, Fake.calls = routes, []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def device(self, driver, creds):
        f = Path(self.tmp.name) / "creds.json"
        f.write_text(json.dumps(creds))
        return Device(name="dev1", driver=driver, url=f"http://127.0.0.1:{self.server.server_address[1]}",
                      credentials_file=str(f))


class OPNsenseTest(DeviceTestBase):
    STATUS: ClassVar[dict] = {"status": "update", "status_msg": "There are 3 updates available",
                              "product_id": "opnsense",
              "product": {"product_version": "25.1.3", "product_latest": "25.1.4"},
              "upgrade_packages": [{"name": "a"}], "needs_reboot": "1"}

    def test_status_uses_api_key_and_reports_update(self):
        self.serve({("GET", "/api/core/firmware/status"): lambda h, b: (200, self.STATUS, {})})
        st = devices.driver_for(self.device("opnsense", {"key": "K", "secret": "S"})).status()
        got = (st.version, st.latest, st.update_available, st.needs_reboot)
        self.assertEqual(got, ("25.1.3", "25.1.4", True, True))
        auth = Fake.calls[0][3]["Authorization"]
        self.assertEqual(base64.b64decode(auth.split()[1]).decode(), "K:S")

    def test_up_to_date(self):
        st = {"status": "none", "product": {"product_version": "25.1.4"}}
        self.serve({("GET", "/api/core/firmware/status"): lambda h, b: (200, st, {})})
        s = devices.driver_for(self.device("opnsense", {"key": "K", "secret": "S"})).status()
        self.assertEqual((s.update_available, s.latest), (False, "25.1.4"))

    def test_bad_key_is_a_clear_error(self):
        self.serve({("GET", "/api/core/firmware/status"): lambda h, b: (401, {}, {})})
        with self.assertRaisesRegex(devices.DeviceAuthError, "credentials refused"):
            devices.driver_for(self.device("opnsense", {"key": "K", "secret": "bad"})).status()

    def test_update_follows_the_log_until_done(self):
        states = iter([{"status": "running", "log": "fetching\n"}, {"status": "done", "log": "fetching\ninstalled\n"}])
        self.serve({("GET", "/api/core/firmware/status"): lambda h, b: (200, self.STATUS, {}),
                    ("POST", "/api/core/firmware/check"): lambda h, b: (200, {"status": "ok"}, {}),
                    ("POST", "/api/core/firmware/update"): lambda h, b: (200, {"status": "ok"}, {}),
                    ("GET", "/api/core/firmware/upgradestatus"):
                        lambda h, b: (200, next(states, {"status": "done", "log": "fetching\ninstalled\n"}), {})})
        drv = devices.driver_for(self.device("opnsense", {"key": "K", "secret": "S"}))
        drv.poll_s = 0
        log = io.StringIO()
        self.assertEqual(drv.update(log), 0)
        self.assertIn("installed", log.getvalue())
        self.assertIn(("POST", "/api/core/firmware/update"), [(c[0], c[1]) for c in Fake.calls])

    def test_missing_credentials(self):
        self.serve({})
        with self.assertRaisesRegex(devices.DeviceError, "missing secret"):
            devices.driver_for(self.device("opnsense", {"key": "K"}))


FIRMVER = "3.0.0.4"  # site-data: ok (a firmware version, not an address)


class AsusTest(DeviceTestBase):
    def routes(self, flag="1"):
        def login(h, body):
            auth = parse_qs(body)["login_authorization"][0]
            if base64.b64decode(auth).decode() != "admin:pw":
                return 200, {"error_status": "3", "captcha_on": "0"}, {}
            return 200, {"asus_token": "TOK"}, {}

        nvram = {"productid": "RT-AX88U", "firmver": FIRMVER, "buildno": "388", "extendno": "24353-gabc12",
                 "webs_state_flag": flag, "webs_state_info": "3.0.0.4_388_25000", "webs_state_update": "1"}

        def appget(h, body):
            # Like the firmware: only the names asked for, read from a form body.
            assert h.headers.get("Cookie") == "asus_token=TOK"
            names = re.findall(r"nvram_get\((\w+)\)", parse_qs(body)["hook"][0])
            return 200, {n: nvram[n] for n in names}, {}
        return {("POST", "/login.cgi"): login, ("POST", "/appGet.cgi"): appget,
                ("GET", "/Logout.asp"): lambda h, b: (200, b"", {}),
                ("POST", "/apply.cgi"): lambda h, b: (200, b"", {})}

    def test_status_and_always_logs_out(self):
        self.serve(self.routes())
        st = devices.driver_for(self.device("asuswrt", {"username": "admin", "password": "pw"})).status()
        self.assertEqual((st.model, st.version, st.update_available), ("RT-AX88U", "3.0.0.4.388_24353-gabc12", True))
        self.assertEqual(st.latest, "3.0.0.4.388_25000")
        self.assertEqual(Fake.calls[-1][1], "/Logout.asp")

    def test_wrong_password(self):
        self.serve(self.routes())
        with self.assertRaisesRegex(devices.DeviceAuthError, "wrong username or password"):
            devices.driver_for(self.device("asuswrt", {"username": "admin", "password": "nope"})).status()

    def test_up_to_date(self):
        self.serve(self.routes(flag="0"))
        st = devices.driver_for(self.device("asuswrt", {"username": "admin", "password": "pw"})).status()
        self.assertEqual((st.update_available, st.latest), (False, "3.0.0.4.388_24353-gabc12"))


class OmadaTest(DeviceTestBase):
    def test_md5_login_then_status(self):
        want = hashlib.md5(b"pw").hexdigest().upper()

        def login(h, body):
            ok = parse_qs(body).get("password", [""])[0] == want
            return 200, {"success": ok, "error": 0}, ({"Set-Cookie": "JSESSIONID=abc; Path=/"} if ok else {})
        self.serve({("GET", "/"): lambda h, b: (200, b"<html>", {}), ("POST", "/"): login,
                    ("GET", "/data/status.device.json"): lambda h, b: (200, {"error": 0, "success": True, "data": {
                        "deviceModel": "EAP245", "hardwareVersion": "1.0",
                        "firmwareVersion": "5.1.0 Build 20230101"}}, {})})
        st = devices.driver_for(self.device("omada_eap", {"username": "admin", "password": "pw"})).status()
        self.assertEqual((st.model, st.version, st.update_available), ("EAP245", "5.1.0 Build 20230101", None))
        self.assertFalse(omada_eap.OmadaEAP.supports_update)


class HomeAssistantTest(DeviceTestBase):
    def routes(self, installed):
        def upd(eid, title, cur, new, sf):
            on = eid not in installed and cur != new
            return {"entity_id": eid, "state": "on" if on else "off",
                    "attributes": {"title": title, "installed_version": new if eid in installed else cur,
                                   "latest_version": new, "supported_features": sf, "in_progress": False}}

        def states():
            return [upd("update.home_assistant_core_update", "Home Assistant Core", "2026.10.1", "2026.11.0", 15),
                    upd("update.mosquitto_broker_update", "Mosquitto broker", "6.4", "6.5", 1),
                    upd("update.home_assistant_operating_system_update", "Home Assistant Operating System",
                        "16.2", "16.2", 15),
                    {"entity_id": "sensor.temperature", "state": "21"}]

        def auth(fn):
            def wrap(h, body):
                assert h.headers["Authorization"] == "Bearer tok"
                return fn(h, body)
            return wrap

        def install(h, body):
            installed.append(json.loads(body)["entity_id"])
            return 200, [], {}
        routes = {("GET", "/api/config"): auth(lambda h, b: (200, {"version": "2026.10.1"}, {})),
                  ("GET", "/api/states"): auth(lambda h, b: (200, states(), {})),
                  ("POST", "/api/services/update/install"): auth(install),
                  ("POST", "/api/services/hassio/host_reboot"): auth(lambda h, b: (200, [], {}))}
        for e in ("update.home_assistant_core_update", "update.mosquitto_broker_update"):
            routes[("GET", f"/api/states/{e}")] = auth(lambda h, b, e=e: (200, next(x for x in states()
                                                                                       if x["entity_id"] == e), {}))
        return routes

    def test_status_update_order_and_reboot(self):
        installed = []
        self.serve(self.routes(installed))
        drv = devices.driver_for(self.device("homeassistant", {"token": "tok"}))
        st = drv.status()
        self.assertEqual((st.model, st.version, st.latest, st.update_available), ("Home Assistant OS", "2026.10.1",
                                                                                  "2026.11.0", True))
        self.assertEqual(len(st.notes), 2)
        drv.poll_s = 0
        log = io.StringIO()
        self.assertEqual(drv.update(log), 0)
        self.assertEqual(installed, ["update.mosquitto_broker_update", "update.home_assistant_core_update"])
        bodies = [json.loads(c[2]) for c in Fake.calls if c[1] == "/api/services/update/install"]
        self.assertEqual([b.get("backup") for b in bodies], [None, True])     # only where it is supported
        self.assertEqual(drv.reboot(io.StringIO()), 0)
        self.assertEqual(Fake.calls[-1][1], "/api/services/hassio/host_reboot")


class FortiGateTest(DeviceTestBase):
    def test_patch_releases_only(self):
        state = {"version": "v7.4.3"}

        def img(i, a, b, c):
            return {"id": i, "version": f"v{a}.{b}.{c}", "major": a, "minor": b, "patch": c}

        def firmware(h, b):
            return 200, {"results": {"current": {"version": state["version"], "major": 7, "minor": 4,
                                                 "patch": int(state["version"].rsplit(".", 1)[1])},
                                     "available": [img("img744", 7, 4, 4), img("img745", 7, 4, 5),
                                                   img("img760", 7, 6, 0)]}}, {}

        def upgrade(h, b):
            assert json.loads(b) == {"source": "fortiguard", "filename": "img745"}
            state["version"] = "v7.4.5"
            return 200, {"results": {"status": "success"}}, {}
        model = {"model_name": "FortiGate", "model_number": "60F"}
        self.serve({("GET", "/api/v2/monitor/system/status"): lambda h, b: (200, {
                        "results": model, "version": state["version"]}, {}),
                    ("GET", "/api/v2/monitor/system/firmware"): firmware,
                    ("POST", "/api/v2/monitor/system/firmware/upgrade"): upgrade})
        drv = devices.driver_for(self.device("fortigate", {"token": "k"}))
        st = drv.status()
        self.assertEqual((st.model, st.version, st.latest, st.update_available), ("FortiGate 60F", "v7.4.3", "v7.4.5",
                                                                                  True))
        self.assertIn("7.6", st.notes[0])                                    # reported, not installed
        drv.poll_s = 0
        self.assertEqual(drv.update(io.StringIO()), 0)
        self.assertEqual(drv.status().update_available, False)


class CapabilitiesTest(unittest.TestCase):
    def test_flags(self):
        self.assertEqual(devices.capabilities(Device(driver="opnsense")), {"update": True, "reboot": True})
        self.assertEqual(devices.capabilities(Device(driver="omada_eap")), {"update": False, "reboot": False})
        self.assertTrue(asuswrt.AsusWrt.supports_update and opnsense.OPNsense.supports_reboot)



class DeviceJobTest(unittest.TestCase):
    def test_missing_credentials_is_a_clean_failure(self):
        from unittest import mock

        from patchyard import jobs
        from tests.helpers import make_config
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(paths__state=tmp)
            cfg.devices = [Device(name="ap1", driver="asuswrt", url="https://ap1.example.com",
                                  credentials_file=f"{tmp}/secrets/ap1.json")]
            jm = jobs.JobManager(cfg)
            log = io.StringIO()
            with mock.patch.object(jobs.JobManager, "check", return_value=0):
                rc = jm.device_action("ap1", "check")(log)
            self.assertEqual(rc, 1)
            self.assertIn("!! ap1: ", log.getvalue())
            self.assertIn("ap1.json: not found", log.getvalue())


if __name__ == "__main__":
    unittest.main()


class MoreDriversTest(DeviceTestBase):
    def creds(self, **kw):
        return {"username": "u", "password": "p", **kw}

    def test_mikrotik(self):
        resource = {"board-name": "hAP ax3", "version": "7.15 (stable)"}
        update = {"installed-version": "7.15", "latest-version": "7.16", "status": "New version is available"}
        self.serve({("GET", "/rest/system/resource"): lambda h, b: (200, resource, {}),
                    ("GET", "/rest/system/package/update"): lambda h, b: (200, update, {}),
                    ("POST", "/rest/system/package/update/check-for-updates"): lambda h, b: (200, [], {})})
        st = devices.driver_for(self.device("mikrotik", self.creds())).status(check=True)
        self.assertEqual((st.model, st.version, st.latest, st.update_available), ("hAP ax3", "7.15", "7.16", True))

    def test_truenas(self):
        info = {"version": "TrueNAS-SCALE-24.10.1", "system_product": "Mini"}
        avail = {"status": "AVAILABLE", "version": "24.10.2"}
        self.serve({("GET", "/api/v2.0/system/info"): lambda h, b: (200, info, {}),
                    ("POST", "/api/v2.0/update/check_available"): lambda h, b: (200, avail, {})})
        st = devices.driver_for(self.device("truenas", {"api_key": "k"})).status()
        self.assertEqual((st.version, st.latest, st.update_available), ("TrueNAS-SCALE-24.10.1", "24.10.2", True))
        self.assertEqual(Fake.calls[0][3]["Authorization"], "Bearer k")

    def test_synology_status_and_logout(self):
        def entry(h, b):
            q = parse_qs(h.path.split("?", 1)[1])
            api, method = q["api"][0], q["method"][0]
            if api == "SYNO.API.Auth" and method == "login":
                return 200, {"success": True, "data": {"sid": "S1"}}, {}
            if api == "SYNO.DSM.Info":
                return 200, {"success": True, "data": {"model": "DS220+", "version_string": "DSM 7.2.1-69057"}}, {}
            if api == "SYNO.Core.Upgrade.Server":
                return 200, {"success": True, "data": {"update": {"available": True, "version": "7.2.2"}}}, {}
            return 200, {"success": True, "data": {}}, {}
        self.serve({("GET", "/webapi/entry.cgi"): entry})
        st = devices.driver_for(self.device("synology", self.creds())).status()
        self.assertEqual((st.model, st.update_available, st.latest), ("DS220+", True, "7.2.2"))
        self.assertIn("method=logout", Fake.calls[-1][1])

    def test_synology_two_factor_account_is_refused_clearly(self):
        self.serve({("GET", "/webapi/entry.cgi"): lambda h, b: (200, {"success": False, "error": {"code": 403}}, {})})
        with self.assertRaisesRegex(devices.DeviceAuthError, "two-factor"):
            devices.driver_for(self.device("synology", self.creds())).status()

    def test_unifi_needs_mac_and_reads_the_device(self):
        self.serve({("POST", "/api/auth/login"): lambda h, b: (200, {}, {}),
                    ("POST", "/api/auth/logout"): lambda h, b: (200, {}, {}),
                    ("GET", "/proxy/network/api/s/default/stat/device/aa:bb:cc:dd:ee:ff"): lambda h, b: (
                        200, {"data": [{"model": "U6LR", "version": "6.6.55", "upgradable": True,
                                        "upgrade_to_firmware": "6.6.77", "name": "hall"}]}, {})})
        dev = self.device("unifi", self.creds())
        with self.assertRaisesRegex(devices.DeviceError, "options.mac"):
            devices.driver_for(dev)
        dev.options = {"mac": "AA:BB:CC:DD:EE:FF"}
        st = devices.driver_for(dev).status()
        self.assertEqual((st.model, st.version, st.latest, st.update_available), ("U6LR", "6.6.55", "6.6.77", True))

    def test_openwrt(self):
        def ubus(h, body):
            req = json.loads(body)
            obj = req["params"][1]
            if obj == "session":
                ok = req["params"][3]["password"] == "p"
                return 200, {"result": [0, {"ubus_rpc_session": "abc"}] if ok else [6]}, {}
            return 200, {"result": [0, {"model": "GL-MT6000", "release": {"description": "OpenWrt 23.05.5"}}]}, {}
        self.serve({("POST", "/ubus"): ubus})
        st = devices.driver_for(self.device("openwrt", self.creds())).status()
        self.assertEqual((st.model, st.version, st.update_available), ("GL-MT6000", "OpenWrt 23.05.5", None))
        with self.assertRaises(devices.DeviceAuthError):
            devices.driver_for(self.device("openwrt", self.creds(password="bad"))).status()

    def test_exec_runs_configured_commands(self):
        self.serve({})
        dev = Device(name="nas", driver="exec", url="", options={
            "status_command": 'printf \'{"model": "X", "version": "1", "latest": "2", "update_available": true}\'',
            "reboot_command": "echo rebooting $PATCHYARD_DEVICE"})
        st = devices.driver_for(dev).status()
        self.assertEqual((st.model, st.latest, st.update_available), ("X", "2", True))
        self.assertEqual(devices.capabilities(dev), {"update": False, "reboot": True})
        log = io.StringIO()
        self.assertEqual(devices.driver_for(dev).reboot(log), 0)
        self.assertIn("rebooting nas", log.getvalue())
        with self.assertRaisesRegex(devices.DeviceError, "not configured"):
            devices.driver_for(dev).update(io.StringIO())
